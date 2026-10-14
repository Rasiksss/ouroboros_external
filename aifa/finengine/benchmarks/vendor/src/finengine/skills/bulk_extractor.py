"""Metric extraction for Finengine pipelines.

Current `finengine` / `finengine_dev` registry mode uses full-context extraction:
the prompt receives the metric registry from `metrics.json`, asks for raw table
headers/cell values, and Python normalizes periods, values and calculated
metrics. Segment breakdowns are a second full-context LLM stage.

The older per-metric RAG path remains in this module for `finengine_rag` and
legacy validation. Its retrieval still uses METRIC_EXPANSION_ALIASES,
METRIC_ALIASES_EXTRA and METRIC_PROMPT_HINTS.
"""
from __future__ import annotations
import asyncio
import contextlib
import logging
import os
from pathlib import Path
import re
from typing import Callable, Optional, AsyncGenerator

os.environ.pop("CLAUDECODE", None)

from finengine.llm import openrouter as llm  # noqa: E402
from finengine.skills.metric_registry import (
    EXTRACTABLE_NON_FINANCIAL,
    EXTRACTABLE_BANK,
    CALCULATED_NON_FINANCIAL,
    CALCULATED_BANK,
    canonical_document_type_for_processing_mode,
    extractable_for_processing_mode,
    processing_mode_for_document,
    source_relevant_calculated_defs,
    supports_metric_extraction,
)
from finengine.skills.periods import (
    DURATION_KIND,
    FiscalPeriodContext,
    POINT_KIND,
    detect_period_candidates,
    normalize_incomplete_duration_label,
    normalize_indian_fiscal_period_label,
    normalize_period_label,
    period_kind_for_enum,
    period_sort_key,
    previous_period_label,
    sort_periods,
)
from finengine.skills.period_validation import normalize_metric_period

_LOG = logging.getLogger(__name__)

# Bank/non-financial detection now uses phrases derived from the metric
# registry (see _registry_phrases_for_segment / _detect_company_type below).


def _write_llm_debug_file(debug_dir: Optional[Path], name: str, content: object) -> None:
    if debug_dir is None:
        return
    try:
        import json as _json

        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / name
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_text(_json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _is_total_segment_label(segment: str) -> bool:
    value = (segment or "").strip().lower().replace("ё", "е")
    return value in {"total", "segment total", "total segment", "итого", "всего"} or value.startswith("total ")


def _period_value_signature(period_values: list[dict]) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for pv in period_values:
        value = pv.get("value")
        if value is None:
            continue
        out.append((str(pv.get("period") or ""), str(value).strip()))
    return tuple(sorted(out))


SOURCE_FORM_BALANCE = "Баланс"
SOURCE_FORM_PNL = "P&L"
SOURCE_FORM_CASH_FLOW = "ОДДС"
SOURCE_FORM_EQUITY = "ОиК"
SOURCE_FORM_NOTES = "Примечания"
_SOURCE_FORM_SCAN_CHARS = 2400

_SOURCE_FORM_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        SOURCE_FORM_BALANCE,
        (
            "отчет о финансовом положении",
            "отчет специального назначения о финансовом положении",
            "отчет о финансовом состоянии",
            "балансовый отчет",
            "бухгалтерский баланс",
            "statement of financial position",
            "statements of financial position",
            "statement of assets and liabilities",
            "statements of assets and liabilities",
            "balance sheet",
            "balance sheets",
            "statement of financial condition",
            "statements of financial condition",
        ),
    ),
    (
        SOURCE_FORM_PNL,
        (
            "отчет о прибылях и убытках",
            "отчет о прибылях или убытках",
            "отчет о прибыли и убытке",
            "отчет о прибыли или убытке",
            "отчет специального назначения о прибыли или убытке",
            "отчет о финансовых результатах",
            "отчет о совокупном доходе",
            "отчет о совокупных доходах",
            "отчет о прочем совокупном доходе",
            "statement of profit or loss",
            "statements of profit or loss",
            "statement of profit or loss and other comprehensive income",
            "statements of profit or loss and other comprehensive income",
            "statement of profit and loss",
            "statements of profit and loss",
            "profit and loss account",
            "consolidated profit and loss account",
            "standalone profit and loss account",
            "statement of comprehensive income",
            "statements of comprehensive income",
            "income statement",
            "statements of income",
            "statement of income",
            "statement of earnings",
            "statements of earnings",
            "statement of operations",
            "statements of operations",
            "statement of financial results",
        ),
    ),
    (
        SOURCE_FORM_CASH_FLOW,
        (
            "отчет о движении денежных средств",
            "отчет специального назначения о движении денежных средств",
            "отчет о денежных потоках",
            "отчет о потоках денежных средств",
            "statement of cash flows",
            "statements of cash flows",
            "cash flow statement",
            "cash flows statement",
            "statement of cash flow",
        ),
    ),
    (
        SOURCE_FORM_EQUITY,
        (
            "отчет об изменениях в капитале",
            "отчет об изменении в капитале",
            "отчет об изменениях капитала",
            "отчет об изменении капитала",
            "отчет об изменениях собственного капитала",
            "отчет об изменениях в собственном капитале",
            "отчет об изменении собственного капитала",
            "отчет об изменении в собственном капитале",
            "отчет специального назначения об изменениях в капитале",
            "отчет специального назначения об изменении капитала",
            "statement of changes in equity",
            "statements of changes in equity",
            "statement of changes in other equity",
            "statements of changes in other equity",
            "statement of changes in shareholders equity",
            "statements of changes in shareholders equity",
            "statement of shareholders equity",
            "statement of shareholders' equity",
            "statement of stockholders equity",
            "statement of stockholders' equity",
        ),
    ),
)

_RU_STATEMENT_PREFIX = r"отчет(?:\s+\S+){0,10}"
_SOURCE_FORM_REGEX_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        SOURCE_FORM_BALANCE,
        (
            re.compile(rf"{_RU_STATEMENT_PREFIX}\s+о\s+финансовом\s+(?:положении|состоянии)"),
        ),
    ),
    (
        SOURCE_FORM_PNL,
        (
            re.compile(
                rf"{_RU_STATEMENT_PREFIX}\s+о\s+(?:"
                r"прибылях\s+(?:и|или)\s+убытках|"
                r"прибыли\s+(?:и|или)\s+убытке|"
                r"финансовых\s+результатах|"
                r"совокупн(?:ом|ых)\s+доход(?:е|ах)|"
                r"прочем\s+совокупном\s+доходе"
                r")"
            ),
        ),
    ),
    (
        SOURCE_FORM_CASH_FLOW,
        (
            re.compile(
                rf"{_RU_STATEMENT_PREFIX}\s+о\s+(?:"
                r"движении\s+денежных\s+средств|"
                r"денежных\s+потоках|"
                r"потоках\s+денежных\s+средств"
                r")"
            ),
        ),
    ),
    (
        SOURCE_FORM_EQUITY,
        (
            re.compile(
                rf"{_RU_STATEMENT_PREFIX}\s+об?\s+изменени(?:ях|и)\s+"
                r"(?:в\s+)?(?:собственн(?:ом|ого|ых)\s+)?(?:капитал(?:е|а)|средств(?:ах|а)?)"
            ),
        ),
    ),
)


def _source_form_norm(text: str) -> str:
    value = (text or "").lower().replace("ё", "е").replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _role_allows_source_forms(role: str | None) -> bool:
    value = _source_form_norm(role or "")
    return value in {
        "",
        "default",
        "general",
        "other",
        "общая",
        "общий",
        "без роли",
        "cib",
        "сиб",
        "baifp",
        "bafp",
        "баифп",
    }


def _text_looks_like_ifrs(text: str, reporting_standard: str = "") -> bool:
    head = _source_form_norm((text or "")[:200_000])
    if any(
        marker in head
        for marker in (
            "u.s. gaap",
            "us gaap",
            "united states generally accepted accounting principles",
        )
    ):
        return False
    return any(
        marker in head
        for marker in (
            "мсфо",
            "ifrs",
            "ind as",
            "ind-as",
            "indian accounting standard",
            "indian accounting standards",
            "recognition and measurement principles laid down in the indian accounting standard",
            "accounting standards prescribed under section 133",
            "accounting standards specified under section 133",
            "accounting standard prescribed under section 133",
            "accounting standard specified under section 133",
            "generally accepted accounting principles in india",
            "banking regulation act, 1949",
            "reserve bank of india",
            "international financial reporting standards",
            "international accounting standards",
        )
    )


def _iter_paginated_pages(text: str) -> list[tuple[int, str]]:
    pages: list[tuple[int, str]] = []
    page_re = re.compile(r"(?ms)^===PAGE\s+(\d+)===\s*\n?(.*?)(?=^===PAGE\s+\d+===|\Z)")
    for match in page_re.finditer(text or ""):
        try:
            page = int(match.group(1))
        except Exception:
            continue
        if page > 0:
            pages.append((page, match.group(2) or ""))
    return pages


def _detect_source_form_for_page(page_text: str, *, scan_chars: int = _SOURCE_FORM_SCAN_CHARS) -> str | None:
    head = _source_form_norm((page_text or "")[:scan_chars])
    if not head:
        return None
    if any(marker in head[:800] for marker in ("содержание", "оглавление", "table of contents", "contents")):
        return None
    for label, patterns in _SOURCE_FORM_PATTERNS:
        if any(pattern in head for pattern in patterns):
            return label
    for label, patterns in _SOURCE_FORM_REGEX_PATTERNS:
        if any(pattern.search(head) for pattern in patterns):
            return label
    return None


def _build_source_form_by_page(
    text: str,
    *,
    reporting_standard: str = "",
    role: str | None = None,
) -> dict[int, str]:
    if not _role_allows_source_forms(role):
        return {}
    if not _text_looks_like_ifrs(text, reporting_standard):
        return {}
    out: dict[int, str] = {}
    for page, page_text in _iter_paginated_pages(text):
        out[page] = _detect_source_form_for_page(page_text) or SOURCE_FORM_NOTES
    return out


def _source_form_for_page(page: object, source_form_by_page: dict[int, str]) -> str:
    try:
        page_int = int(page or 0)
    except Exception:
        page_int = 0
    if page_int <= 0 or not source_form_by_page:
        return ""
    return source_form_by_page.get(page_int) or SOURCE_FORM_NOTES


def _apply_metric_source_forms(event: dict, source_form_by_page: dict[int, str]) -> dict:
    if not source_form_by_page or event.get("type") != "metric":
        return event

    event = dict(event)
    first_form = _source_form_for_page(event.get("page"), source_form_by_page)
    period_values: list[dict] = []
    for pv in event.get("period_values") or []:
        if not isinstance(pv, dict):
            continue
        item = dict(pv)
        form = _source_form_for_page(item.get("page") or event.get("page"), source_form_by_page)
        if form:
            item["source_form"] = form
            first_form = first_form or form
        period_values.append(item)
    if period_values:
        event["period_values"] = period_values
    if first_form:
        event["source_form"] = first_form
    return event


# ─────────────────────────────────────────────────────────────────────────────
# Curated aliases — ported and expanded from validator.py
# ─────────────────────────────────────────────────────────────────────────────

METRIC_ALIASES_EXTRA: dict[str, list[str]] = {
    "LONG_TERM_ROU": [
        "долгосрочные активы в форме права пользования", "долгосрочная часть ППА",
        "non-current right-of-use assets",
    ],
    "SHORT_TERM_ROU": [
        "краткосрочные активы в форме права пользования", "краткосрочная часть ППА",
        "current right-of-use assets",
    ],
    "ROU_DEPRECIATION": [
        "амортизация активов в форме права пользования", "износ ППА",
        "depreciation of right-of-use assets", "amortization of operating lease right-of-use assets",
    ],
    "CAPITALIZED_INTEREST": [
        "капитализированные затраты по займам незавершённое строительство",
        "капитализированные процентные расходы", "капитализированные проценты",
        "проценты включены в стоимость активов", "капитализировано процентов",
        "borrowing costs capitalized construction",
    ],
    "LONG_TERM_LEASE_LIABILITIES": [
        "долгосрочные обязательства по аренде", "обязательства по аренде долгосрочные",
        "долгосрочная часть обязательств по аренде", "lease liabilities non-current",
        "обязательства по аренде – долгосрочные",
    ],
    "SHORT_TERM_LEASE_LIABILITIES": [
        "краткосрочные обязательства по аренде", "обязательства по аренде краткосрочные",
        "текущая часть обязательств по аренде", "lease liabilities current",
        "краткосрочные арендные обязательства",
    ],
    "PRINCIPAL_REPAYMENT_OF_LEASE_LIABILITIES": [
        "погашение обязательств по аренде", "погашение основного долга по аренде",
        "principal payments on lease liabilities",
    ],
    "INTEREST_PAID_LEASE_LIABILITIES": [
        "уплаченные проценты по лизингу", "проценты уплаченные по аренде",
        "interest paid on lease liabilities",
    ],
    "INVENTORIES": [
        "товарно-материальные запасы итого", "ТМЗ запасы сырьё материалы готовая продукция",
        "итого запасы", "inventories total",
    ],
    "LONG_TERM_DEBT": [
        "долгосрочные кредиты и займы итого нетто", "кредиты банки финансовые учреждения долгосрочные",
        "долгосрочные заёмные средства", "долгосрочные обязательства по займам",
        "long-term borrowings", "долгосрочная часть кредитов",
    ],
    "SHORT_TERM_BORROWINGS": [
        "краткосрочные кредиты займы текущая часть долгосрочных итого",
        "краткосрочные кредиты и займы", "краткосрочные заёмные средства",
        "short-term borrowings", "текущая часть долгосрочных займов",
        "В российских рублях В евро банки финансовые учреждения займы",
        "текущая часть долгосрочных кредитов и займов банки рубли евро",
    ],
    "DEFERRED_TAX_ASSETS": [
        "отражено в отчёте о финансовом положении отложенные налоговые активы",
        "отложенный налоговый актив", "ОНА", "deferred tax asset",
        "отложенные налоговые активы обязательства нетто отчёт",
    ],
    "DEFERRED_TAX_LIABILITIES": [
        "отражено в отчёте о финансовом положении отложенные налоговые обязательства",
        "отложенное налоговое обязательство", "ОНО", "deferred tax liability",
        "отложенные налоговые обязательства нетто отчёт финансовом положении",
    ],
    "ACCOUNTS_RECEIVABLE": [
        "дебиторская задолженность покупателей", "торговая дебиторская задолженность",
        "торговая и прочая дебиторская задолженность", "trade receivables",
        "торговая и прочая дебиторская",
    ],
    "ACCOUNTS_PAYABLE": [
        "кредиторская задолженность поставщиков", "торговая кредиторская задолженность",
        "торговая и прочая кредиторская задолженность", "trade payables",
        "дивиденды к уплате прочая кредиторская задолженность итого",
    ],
    "OPERATING_EXPENSES": [
        "итого операционных расходов", "всего операционных расходов",
        "total operating expenses", "совокупные операционные расходы",
        "коммерческие общехозяйственные административные расходы",
        "general and administrative expense", "selling general and administrative",
        "marketing general and administrative", "SG&A", "G&A",
    ],
    "COSTS_EXPENSES": [
        "себестоимость реализованной продукции", "себестоимость продаж",
        "себестоимость реализации", "cost of sales", "стоимость продаж",
        "total costs and expenses", "total cost and expenses",
        "cost of products", "cost of services",
    ],
    "TOTAL_CURRENT_ASSETS": [
        "итого оборотные активы", "всего оборотные активы",
        "total current assets", "итого текущие активы",
    ],
    "TOTAL_CURRENT_LIABILITIES": [
        "итого краткосрочные обязательства", "всего краткосрочные обязательства",
        "total current liabilities", "итого текущие обязательства",
    ],
    "COMMITTED_CREDIT_FACILITIES": [
        "неиспользованные кредитные линии", "доступные кредитные линии",
        "undrawn committed facilities", "available credit lines",
    ],
    "BANK_DEPOSITS": [
        "депозиты срочные банковские", "краткосрочные депозиты",
        "time deposits", "short-term bank deposits", "bank deposits", "short-term deposits",
        "краткосрочные финансовые вложения депозиты",
    ],
    "ASSET_PURCHASES": [
        "покупка основных фондов", "приобретение внеоборотных активов",
        "purchases of property plant and equipment", "additions to PP&E",
        "приобретение основных средств",
    ],
    "CAPEX": [
        "капитальные вложения", "capital expenditures", "capex",
        "капитальные затраты", "CAPEX",
        "payments to acquire property plant and equipment",
        "capital expenditures total",
    ],
    "NET_OPERATING_CASH_FLOW": [
        "чистые денежные средства от операционной деятельности",
        "net cash from operating activities", "операционная деятельность итого",
    ],
    "DOUBTFUL_ACCOUNTS_ALLOWANCE": [
        "резерв под ожидаемые кредитные убытки дебиторская",
        "ECL allowance receivables", "оценочный резерв ECL дебиторская",
    ],
    "SHARE_CAPITAL": [
        "уставный капитал", "акционерный капитал", "share capital",
        "authorized capital", "обыкновенные акции уставный капитал",
        "common stock", "capital stock",
    ],
    "SHARE_PREMIUM": [
        "эмиссионный доход", "добавочный капитал эмиссия",
        "share premium", "additional paid-in capital",
    ],
    "TREASURY_SHARES": [
        "собственные акции выкупленные у акционеров", "выкупленные акции",
        "treasury shares", "собственные выкупленные акции", "treasury stock",
    ],
    "RETAINED_EARNINGS": [
        "нераспределённая прибыль", "нераспределенная прибыль (убыток)",
        "retained earnings", "накопленная прибыль", "accumulated deficit",
    ],
    "NON_CONTROL_INTEREST": [
        "неконтрольные доли участия", "доля меньшинства",
        "non-controlling interest", "minority interest",
        "неконтролирующие акционеры",
    ],
    "TOTAL_ASSOCIATE_INVESTMENTS": [
        "инвестиции в ассоциированные и совместные предприятия итого",
        "вложения в ассоциированные общества итого",
        "total investments in associates", "equity-accounted investees",
        "equity method investment", "investments in associates",
    ],
    "SECURITIES_ASSETS": [
        "финансовые инструменты актив", "финансовые вложения",
        "инвестиционные ценные бумаги", "financial assets",
        "долговые инструменты акции финансовые активы",
        "marketable securities", "short-term investments", "available-for-sale securities",
    ],
    "OTHER_ASSETS": [
        "прочие активы итого", "прочие долгосрочные активы",
        "other assets", "прочие внеоборотные активы",
        "prepaid expenses and other assets",
    ],
    "SECURITIES_LIABILITIES": [
        "финансовые инструменты обязательства", "финансовые обязательства",
        "financial liabilities", "долговые финансовые обязательства",
    ],
    "OTHER_LIABILITIES": [
        "прочие обязательства итого", "прочие долгосрочные обязательства",
        "other liabilities", "прочие краткосрочные обязательства",
        "accrued and other liabilities", "other long-term liabilities",
    ],
    "EBITDA": [
        "EBITDA", "прибыль до вычета процентов налогов износа амортизации",
        "earnings before interest taxes depreciation amortization",
        "скорректированная EBITDA", "adjusted EBITDA",
        "EBITDA по сегменту", "segment EBITDA", "EBITDA total",
    ],
    "EPS": [
        "прибыль на одну обыкновенную акцию", "базовая прибыль на акцию",
        "earnings per share", "EPS", "базовый EPS", "basic earnings per share",
    ],
    "DIVIDENDS": [
        "дивиденды объявленные", "объявленные дивиденды на акцию",
        "dividends declared", "дивиденды к выплате объявленные",
    ],
    "DIVIDENDS_RECEIVED": [
        "дивиденды полученные", "полученные дивиденды",
        "dividends received", "дивиденды от дочерних и ассоциированных",
    ],
    "COMBINED_ASSET_PURCHASES": [
        "приобретение основных средств и нематериальных активов",
        "покупка основных средств нематериальных активов итого",
        "purchases of property plant equipment and intangible assets",
        "капитальные вложения ОС и НМА",
    ],
    "ADJUSTMENTS_OPERATING_CASH_FLOWS": [
        "корректировки операционного денежного потока",
        "adjustments for non-cash items",
        "корректировки на неденежные статьи амортизация",
    ],
    # ── Bank aliases (synced with «Извлекаемые показатели» registry) ────────
    # Balance sheet: Assets
    "CASH_EQUIVALENTS": [
        "денежные средства и их эквиваленты", "денежные средства и эквиваленты денежных средств",
        "денежные средства и денежные эквиваленты", "cash and cash equivalents",
        "денежные средства и их эквиваленты на конец периода",
        "cash and cash equivalents at end of period",
    ],
    "CASH_AND_CASH_BALANCES_WITH_CB": [
        "обязательные резервы на счетах в центральных банках",
        "обязательные резервы на счетах в ЦБ РФ",
        "обязательные резервы на счетах в Центральном банке",
        "денежные средства и остатки в центральном банке",
        "cash and balances with central bank",
    ],
    "FUNDS_FROM_CENTRAL_BANK": [
        "средства, предоставленные ЦБ", "кредиты ЦБ",
        "рефинансирование от центрального банка", "funds from central bank",
        "средства, предоставленные Центральным банком",
    ],
    "INTERBANK_DEPOSITS": [
        "средства в банках", "депозиты в банках", "межбанковские депозиты",
        "due from banks", "placements with banks",
    ],
    "TOTAL_LOANS_TO_CLIENTS": [
        "кредиты клиентам нетто", "кредиты и авансы клиентам",
        "loans and advances to customers", "кредитный портфель",
        "кредитный портфель валовой", "итого кредиты клиентам",
    ],
    "CORPORATE_LOANS": [
        "корпоративные кредиты", "кредиты корпоративным клиентам",
        "кредиты юридическим лицам", "corporate loans",
    ],
    "INDIVIDUALS_LOANS": [
        "розничные кредиты", "кредиты физическим лицам",
        "потребительские кредиты", "retail loans", "individual loans",
    ],
    "LOANS_STAGE1_GROSS": [
        "кредиты Stage 1 валовые", "Stage 1 кредиты gross",
        "стадия 1 валовые кредиты", "stage 1 loans gross",
    ],
    "LOANS_STAGE2_GROSS": [
        "кредиты Stage 2 валовые", "Stage 2 кредиты gross",
        "стадия 2 валовые кредиты", "stage 2 loans gross",
    ],
    "LOANS_STAGE3_GROSS": [
        "кредиты Stage 3 валовые", "Stage 3 кредиты gross",
        "обесцененные кредиты", "stage 3 loans gross", "impaired loans",
    ],
    "ECL_ALLOWANCE_STAGE1": [
        "резерв ECL Stage 1", "оценочный резерв под ожидаемые кредитные убытки Stage 1",
        "ECL Stage 1", "ECL allowance Stage 1",
    ],
    "ECL_ALLOWANCE_STAGE2": [
        "резерв ECL Stage 2", "оценочный резерв под ожидаемые кредитные убытки Stage 2",
        "ECL Stage 2", "ECL allowance Stage 2",
    ],
    "ECL_ALLOWANCE_STAGE3": [
        "резерв ECL Stage 3", "оценочный резерв под ожидаемые кредитные убытки Stage 3",
        "ECL Stage 3", "ECL allowance Stage 3",
    ],
    "SECURITIES_FVPL": [
        "ценные бумаги по справедливой стоимости через прибыль или убыток",
        "финансовые активы оцениваемые по справедливой стоимости через прибыль или убыток",
        "FVPL securities", "securities at FVTPL",
        "финансовые активы по ССПЧПУ",
    ],
    "SECURITIES_FVOCI": [
        "ценные бумаги по справедливой стоимости через прочий совокупный доход",
        "финансовые активы оцениваемые по справедливой стоимости через прочий совокупный доход",
        "FVOCI securities", "securities at FVOCI",
        "финансовые активы по ССПСД",
    ],
    "SECURITIES_AMORTISED_COST": [
        "ценные бумаги по амортизированной стоимости",
        "финансовые активы по амортизированной стоимости",
        "AC securities", "securities at amortised cost",
    ],
    "REPO_PLEDGED_SECURITIES": [
        "ценные бумаги, заложенные по договорам репо",
        "ценные бумаги заложенные по РЕПО",
        "переданные в обеспечение ценные бумаги",
        "securities pledged under repo agreements",
    ],
    "RIGHT_OF_USE_ASSETS": [
        "активы в форме права пользования", "право пользования активами",
        "активы с правом пользования", "right-of-use assets", "ROU assets",
    ],
    "GOODWILL": [
        "гудвил", "деловая репутация", "goodwill",
    ],
    # Balance sheet: Liabilities & Equity
    "CLIENT_DEPOSITS": [
        "средства клиентов", "депозиты клиентов",
        "due to customers", "customer deposits",
    ],
    "CORPORATE_DEPOSITS": [
        "средства корпоративных клиентов", "депозиты корпоративных клиентов",
        "средства юридических лиц", "corporate deposits",
    ],
    "INDIVIDUALS_DEPOSITS": [
        "средства физических лиц", "депозиты физических лиц",
        "вклады физических лиц", "retail deposits", "individual deposits",
    ],
    "GOVERNMENTAL_DEPOSITS": [
        "средства государственных органов власти",
        "депозиты государственных органов",
        "средства государственных и муниципальных органов",
        "government deposits", "state deposits",
    ],
    "INTERBANK_LOANS": [
        "средства банков", "депозиты банков",
        "межбанковские привлечения", "due to banks",
    ],
    "DEBT_SECURITIES_ISSUED": [
        "выпущенные долговые ценные бумаги", "выпущенные облигации",
        "долговые ценные бумаги в обращении", "debt securities issued",
    ],
    "DERIVATIVE_LIABILITIES": [
        "производные финансовые обязательства",
        "производные финансовые инструменты обязательства",
        "деривативы обязательства", "derivative financial liabilities",
    ],
    "JUNIOR_DEBT": [
        "субординированная задолженность", "субординированные займы",
        "субординированный долг", "subordinated debt",
    ],
    "PERPETUAL_BONDS": [
        "бессрочные облигации", "облигации без срока погашения",
        "выпуск бессрочных облигаций", "perpetual bonds",
        "hybrid capital notes", "perpetual notes",
    ],
    "LEASE_LIABILITIES_BANKS": [
        "обязательства по аренде", "lease liabilities",
    ],
    "INSURANCE_LIABILITIES": [
        "обязательства по договорам страхования",
        "страховые резервы", "страховые обязательства",
        "insurance contract liabilities",
    ],
    "TOTAL_EQUITY": [
        "итого собственный капитал", "итого капитал",
        "собственный капитал акционеров", "total equity",
    ],
    # P&L
    "INTEREST_INCOME_ER": [
        "процентные доходы рассчитываемые по методу эффективной процентной ставки",
        "процентные доходы по методу ЭПС", "interest income EIR",
        "процентные доходы",
    ],
    "INTEREST_EXPENSE_ER": [
        "процентные расходы рассчитываемые по методу эффективной процентной ставки",
        "процентные расходы по методу ЭПС", "interest expense EIR",
        "процентные расходы",
    ],
    "NET_INTEREST_INCOME": [
        "чистый процентный доход", "net interest income", "NII",
    ],
    "FEE_AND_COMMISSION_INCOME": [
        "комиссионный доход", "комиссионные доходы",
        "доход от комиссий", "fee and commission income",
    ],
    "FEE_AND_COMMISSION_EXPENSE": [
        "комиссионный расход", "комиссионные расходы",
        "расходы по комиссиям", "fee and commission expense",
    ],
    "NET_COMMISSION_INCOME": [
        "чистый комиссионный доход", "чистые комиссионные доходы",
        "net commission income", "net fee and commission income",
    ],
    "CURRENCY_METALS_INCOME": [
        "чистый доход от операций с иностранными валютами и драгоценными металлами",
        "чистый доход от операций с иностранной валютой",
        "net FX and metals income", "net trading FX",
    ],
    "INSURANCE_INCOME": [
        "выручка по страховой деятельности", "страховая премия",
        "заработанные страховые премии", "insurance revenue",
    ],
    "INSURANCE_EXPENSE": [
        "расходы по страховой деятельности", "страховые выплаты",
        "расходы по страховым случаям", "insurance expense",
    ],
    "NON_INTEREST_INCOME_BANKS": [
        "непроцентный доход", "комиссионные и прочие доходы",
        "non-interest income", "прочие непроцентные доходы",
    ],
    "OTHER_OPERATING_INCOME": [
        "прочие операционные доходы", "прочие доходы",
        "other operating income",
    ],
    "OPERATING_INCOME_BANKS": [
        "операционный доход банков", "операционные доходы",
        "чистый операционный доход", "total operating income",
    ],
    "TOTAL_CREDIT_ALLOWANCE": [
        "отчисления в резервы", "резервы под обесценение кредитов",
        "резервы по ожидаемым кредитным убыткам по кредитам и авансам клиентам",
        "credit impairment losses", "loan loss provisions",
        "расходы на создание резервов под кредитные убытки",
    ],
    "CHANGE_IN_EXPECTED_CREDIT_LOSSES": [
        "изменение резерва по ожидаемым кредитным убыткам",
        "изменение резерва ECL", "движение по резерву ECL",
        "change in expected credit losses",
    ],
    "NET_OTHER_RESERVES": [
        "чистое восстановление прочих резервов",
        "создание восстановление резерва под судебные иски и прочие обязательства",
        "восстановление резерва под кредитные убытки по прочим финансовым активам",
        "net other reserves",
    ],
    "OPERATING_EXPENSES_BANKS": [
        "операционные расходы банков", "непроцентные расходы",
        "операционные и административные расходы", "non-interest expenses",
        "total non-interest expense",
    ],
    "INCOME_TAX": [
        "расход по налогу на прибыль", "налог на прибыль",
        "текущий налог на прибыль", "income tax expense",
    ],
    "NET_INCOME": [
        "чистая прибыль", "чистая прибыль убыток",
        "прибыль за период", "чистая прибыль собственников", "net income",
    ],
    # Cash flow
    "INTEREST_PAID": [
        "уплаченные проценты", "проценты уплаченные",
        "проценты по кредитам уплаченные", "interest paid",
    ],
    "CHANGES_OCF_ASSETS_LIABILITIES": [
        "изменения в оборотном капитале",
        "изменения в операционных активах и обязательствах",
        "changes in operating assets and liabilities",
    ],
    # Capital adequacy & risk
    "RISK_WEIGHTED_ASSETS": [
        "активы взвешенные по риску", "RWA", "risk-weighted assets",
    ],
    "COMMON_EQUITY_TIER_ONE_CAPITAL": [
        "основной капитал первого уровня", "капитал первого уровня",
        "CET1 капитал", "базовый капитал первого уровня",
        "common equity tier 1 capital",
    ],
    "CET1_RATIO_DISCLOSED": [
        "норматив достаточности капитала CET1", "CET1 ratio",
        "норматив CET1", "common equity tier 1 ratio",
    ],
    "TIER1_RATIO_DISCLOSED": [
        "норматив Tier 1", "Tier 1 ratio",
        "норматив достаточности капитала первого уровня", "tier one ratio",
    ],
    "NON_PERFORMING_LOANS_RATIO": [
        "коэффициент неработающих кредитов", "NPL 90+",
        "NPL ratio", "доля неработающих кредитов",
    ],
    "NON_PERFORMING_LOANS_COVERAGE": [
        "коэффициент покрытия неработающих кредитов",
        "покрытие NPL резервами", "NPL coverage ratio",
    ],
}

# Pinpoint page-expansion aliases (high-precision, top_k=2, min_score=0.625)
METRIC_EXPANSION_ALIASES: dict[str, list[str]] = {
    "GOODWILL": [
        "договорные обязательства приобретению основных средств лицензии гудвил нематериальные",
    ],
    "SHORT_TERM_BORROWINGS": [
        "В российских рублях В евро банки финансовые учреждения займы",
    ],
    "DEFERRED_TAX_ASSETS": [
        "отложенные налоговые активы обязательства нетто отчёт",
    ],
    "DEFERRED_TAX_LIABILITIES": [
        "отложенные налоговые активы обязательства нетто отчёт",
    ],
}

# Per-metric prompt hints (disambiguate tricky metrics)
METRIC_PROMPT_HINTS: dict[str, str] = {
    "CAPITALIZED_INTEREST": (
        "\n⚠️ ВАЖНО: нужны ТОЛЬКО капитализированные проценты (включённые в стоимость активов, НЕ из ДДС). "
        "«Проценты уплаченные» в Отчёте о ДДС — это другой показатель, пропусти. "
        "Ищи фразу «капитализированные затраты по займам в размере X» в примечаниях к ОС/НЗС."
    ),
    "LONG_TERM_DEBT": (
        "\n⚠️ В таблице кредитов: строка «Итого долгосрочные кредиты и займы» (нетто, после вычета текущей части) — "
        "это и есть искомый показатель. «Текущая часть долгосрочных» — это отдельная статья, не итог."
    ),
    "SHORT_TERM_BORROWINGS": (
        "\n⚠️ Ищи строку «Итого краткосрочные кредиты и займы» или «Итого» в разделе краткосрочных кредитов "
        "(включая текущую часть долгосрочных). Не путай с отдельными валютными строками (руб, USD, EUR) — нужна итоговая строка."
    ),
    "DEFERRED_TAX_ASSETS": (
        "\n⚠️ Ищи ИМЕННО строку «Отложенные налоговые активы» (не нетто-позицию). "
        "Возвращай значение ровно как в источнике; если в ячейке стоят скобки, не удаляй их."
    ),
    "DEFERRED_TAX_LIABILITIES": (
        "\n⚠️ В таблице «Отражено в отчёте о финансовом положении»: "
        "строка «Отложенные налоговые активы» = X, «Отложенные налоговые обязательства» = (Y), «нетто» = Z. "
        "Тебе нужна строка «Отложенные налоговые обязательства». "
        "Значение в скобках (Y) возвращай со скобками: (5 227) → (5 227). НЕ берёшь нетто.\n"
        "\n❌ КРИТИЧНО — НЕ СУММИРУЙ КОЛОНКИ! Частая ошибка: таблица движения временных разниц "
        "имеет несколько столбцов «Начало года», «Изменение», «Конец года» — модель ошибочно "
        "складывает все три. Бери ТОЛЬКО ОДНУ ЯЧЕЙКУ (конец отчётного года, последний столбец) "
        "из строки «Отложенное налоговое обязательство».\n"
        "Пример Металлинвестбанка: PDF L688 «Отложенное налоговое обязательство (5 032) (2 185) (12)» — "
        "три значения по разным разновидностям активов, ИХ НЕ СУММИРУЮТ. Бери первое (5 032), "
        "а не сумму 7 229. Если непонятно, возьми наибольшее число, не сумму."
    ),
    # NOTE: LONG_TERM_LEASE_LIABILITIES has its definitive hint at the end of
    # this dict (sign-flip block) — meaning is "non-current portion of lease",
    # not the gross total. The old hint here was financially wrong; removed.
    "CASH_EQUIVALENTS": (
        "\n⚠️ OCR-артефакт: если перед суммой стоит число 1–50 (ссылка на примечание), например «16 797» — "
        "«16» это примечание, а «797» — реальная сумма. Верни только сумму. "
        "Для промежуточной отчётности также проверяй ОДДС: строка "
        "«Денежные средства и их эквиваленты, на конец периода» / "
        "«Cash and cash equivalents at end of period» может давать сравнительную "
        "дату, которой нет в балансе."
    ),
    "BANK_DEPOSITS": (
        "\n⚠️ Это банковские депозиты / time deposits / short-term deposits. "
        "Для US GAAP ищи строку «Time deposits» в таблице cash equivalents and short-term investments. "
        "Не заменяй её на cash equivalents или marketable securities. "
        "Для IFRS RU в примечании «Денежные средства и их эквиваленты, краткосрочные финансовые вложения» "
        "бери строку банковских депозитов / краткосрочных финансовых вложений, не номер примечания."
    ),
    "SECURITIES_ASSETS": (
        "\n⚠️ Это ценные бумаги / marketable securities / short-term investments на дату баланса. "
        "Не путай с cash equivalents и time deposits."
    ),
    "GOODWILL": (
        "\n⚠️ Вариант 1 (текстовое примечание): «гудвил составлял 3 642» → верни 3642. "
        "Вариант 2 (таблица НМА): столбцы [Гудвил | Лицензии | Прочие | ПО] — "
        "строка «На 31 декабря {год}» в разделе «Балансовая стоимость» → ПЕРВЫЙ столбец = гудвил."
    ),
    "TOTAL_CURRENT_ASSETS": (
        "\n⚠️ OCR-артефакт: строка «Итого оборотные активы» может выглядеть как «Итого оборотные AKTUBBL» "
        "(латиница вместо кириллицы). Ищи «Итого оборотные» и бери ПЕРВОЕ число."
    ),
    "OPERATING_EXPENSES": (
        "\n⚠️ Это ИТОГОВАЯ сумма операционных расходов в P&L (обычно строка "
        "«Итого операционные расходы» / «Operating expenses» / «Operating costs»). "
        "Если такой итоговой строки нет — суммируй ВСЕ компоненты блока (commercial + "
        "administrative + general + other operating + налоги кроме налога на прибыль + "
        "амортизация если входит в operating). НЕ бери одну подстроку (например только "
        "commercial expenses) — это будет в разы меньше реального значения. "
        "Не путай с «себестоимостью» (cost of sales) — это отдельная строка."
        "\n⚠️ ЗНАК: возвращай расходы ровно как в источнике. "
        "Если в отчёте стоят скобки «(509 192)» или знак минус, сохрани скобки/минус; не возвращай модуль. "
        "В US GAAP 10-Q, если нет строки total operating expenses, но есть "
        "«General and administrative expense» / «Selling, general and administrative» "
        "как отдельная операционная статья после gross profit, извлекай её для "
        "OPERATING_EXPENSES; не путай с «Total costs and expenses». "
        "Для US-чисел с запятой как разделителем тысяч возвращай число без запятой: "
        "2,905 -> 2905."
    ),
    "COSTS_EXPENSES": (
        "\n⚠️ Бери прямую себестоимость/Cost of sales/COGS после выручки. "
        "Если прямые затраты раскрыты компонентами Cost of products/services, "
        "бери релевантный subtotal. Не бери totals, которые смешивают себестоимость "
        "с SG&A/R&D/operating expenses, и не рассчитывай себестоимость как разность "
        "между выручкой и операционной прибылью. "
        "Для US-чисел с запятой как разделителем тысяч возвращай число без запятой: "
        "37,393 -> 37393."
    ),
    "GROSS_PROFIT": (
        "\n⚠️ Если строка «Gross profit» не подписана, но есть очевидный subtotal "
        "после прямых costs of sales/products/services и перед operating expenses, "
        "можно брать этот subtotal. Не вычисляй gross profit, если нет явного subtotal. "
        "Для US-чисел с запятой как разделителем тысяч возвращай число без запятой: "
        "4,852 -> 4852."
    ),
    "ACCOUNTS_RECEIVABLE": (
        "\n⚠️ Бери торговую дебиторскую задолженность / trade accounts receivable net. "
        "Не включай авансы выданные, предоплаты, НДС к возмещению и прочую дебиторскую задолженность, "
        "если метрика запрашивает именно accounts receivable."
    ),
    "DEPRECIATION_AMORTIZATION": (
        "\n⚠️ Для US GAAP cash-flow reconciliation, если отдельно раскрыты "
        "«Depreciation and amortization» и «Amortization of acquisition-related intangibles», "
        "эта метрика должна включать ОБЕ строки суммой. Например 671 + 2,393 = 3,064. "
        "Не бери только первую строку, если acquisition-related amortization раскрыта отдельно."
    ),
    "INCOME_TAX": (
        "\n⚠️ Бери итоговую строку «Income tax provision (benefit)» / «Расход по налогу на прибыль». "
        "Не бери cash paid for taxes и не бери отдельные current/deferred компоненты вместо итоговой строки."
    ),
    "ROU_DEPRECIATION": (
        "\n⚠️ Это амортизация активов в форме права пользования / operating lease right-of-use assets. "
        "В US GAAP cash-flow reconciliation ищи строку "
        "«Amortization of operating lease right-of-use assets». "
        "Не путай с общей depreciation and amortization."
    ),
    "SHARE_PREMIUM": (
        "\n⚠️ Бери additional paid-in capital / эмиссионный доход на конец периода. "
        "В US GAAP это таблица stockholders' equity, строка «Balance, end of period» в блоке Additional paid-in capital."
    ),
    "OTHER_ASSETS": (
        "\n⚠️ Для US GAAP баланса бери строку «Prepaid expenses and other assets» или явно раскрытую "
        "строку other assets на дату баланса. Не бери изменение этой строки из cash-flow."
    ),
    "OTHER_LIABILITIES": (
        "\n⚠️ Для US GAAP баланса бери «Other current liabilities» и/или «Other long-term liabilities» "
        "только если метрика требует соответствующую строку; не бери cash-flow change in accrued liabilities."
    ),
    "TOTAL_ASSOCIATE_INVESTMENTS": (
        "\n⚠️ Ищи equity method investment / investments in associates на дату баланса. "
        "Не путай с purchases of strategic investments в cash-flow."
    ),
    "TOTAL_ASSETS": (
        "\n⚠️ Это итоговая сумма по балансу (обычно строка «Итого активы» внизу раздела активов). "
        "Сумма обычно очень большая (миллионы/миллиарды). "
        "OCR-артефакт: если перед суммой стоит число 1–60 (ссылка на примечание), "
        "например «16 1 234 567» — «16» это номер примечания, реальная сумма «1 234 567». "
        "Двух- или трёхзначные числа НЕ могут быть итогом активов крупной компании — это всегда ссылка."
    ),
    "ASSOCIATE_INVESTMENTS": (
        "\n⚠️ Это инвестиции ТОЛЬКО в ассоциированные компании (без совместных предприятий / JV). "
        "Если в отчёте строка объединённая — «Investments in associates AND joint ventures» / "
        "«Инвестиции в ассоциированные и совместные предприятия» — это уже TOTAL_ASSOCIATE_INVESTMENTS, "
        "а ASSOCIATE_INVESTMENTS = «нет данных». "
        "Бери только если есть отдельная строка под именно ассоциированные (без JV). "
        "Если в отчёте раздел вообще про М&А и JV объединён — верни «нет данных»."
    ),
    # NOTE: SHARES_BUYBACK has its definitive hint at the end of this dict
    # (sign-flip block). The old hint was equivalent — removed to satisfy
    # ruff F601 (no duplicate dict keys).
    "CAPEX": (
        "\n⚠️ Если в отчёте раскрыт управленческий/non-IFRS показатель «капитальные затраты» / "
        "«скорректированные капитальные затраты» / «capital expenditures», бери именно раскрытый итог. "
        "Не заменяй её одной строкой cash-flow «приобретение основных средств». "
        "Если раскрытого итога нет — ищи «Приобретение основных средств» + «Приобретение НМА» "
        "в разделе инвестиционной деятельности как положительные outflows. "
        "В US GAAP также ищи таблицы/MD&A со строкой «Capital expenditures» или "
        "«Total» в таблице capital expenditures; возвращай все раскрытые периоды, "
        "включая Three months/Q2, если они есть."
    ),
    "PERPETUAL_BONDS": (
        "\n⚠️ ИМЕННО бессрочные ОБЛИГАЦИИ (perpetual bonds / hybrid capital notes / perpetual notes) — "
        "отдельная строка в разделе обязательств или гибридного капитала. "
        "НЕ путай: субординированные займы → JUNIOR_DEBT (отдельный показатель); "
        "субординированные депозиты / срочные суборды → тоже JUNIOR_DEBT, не сюда. "
        "Если в отчёте нет ни «бессрочных облигаций», ни «perpetual notes / hybrid» — верни «нет данных»."
    ),
    "RETAINED_EARNINGS": (
        "\n⚠️ Ищи строку «Нераспределённая прибыль» или «Нераспределённая прибыль (убыток)» в разделе капитала. "
        "Не путай с «Чистой прибылью за год» из отчёта о прибылях и убытках. Нужен остаток на конец периода. "
        "В US GAAP это таблица stockholders' equity, строка «Balance, end of period» "
        "в блоке Retained earnings / accumulated deficit."
    ),
    "NON_CONTROL_INTEREST": (
        "\n⚠️ Строка «Неконтрольные доли участия» или «Доля меньшинства» в разделе «Итого капитал». "
        "Это ОТДЕЛЬНАЯ строка от «Итого капитал, причитающийся акционерам материнской компании»."
    ),
    "SHARE_CAPITAL": (
        "\n⚠️ Строка «Уставный капитал» в разделе капитала. Обычно небольшая сумма (номинальная стоимость акций). "
        "Не путай с «Итого собственный капитал» — нужен только уставный капитал. "
        "В US GAAP это таблица stockholders' equity, строка «Balance, end of period» в блоке Common stock."
    ),
    "TREASURY_SHARES": (
        "\n⚠️ Строка «Собственные акции» или «Выкупленные акции» в разделе капитала. "
        "Обычно отрицательное число (уменьшает капитал). Верни значение со знаком из отчётности. "
        "В US GAAP это treasury stock, строка «Balance, end of period», не строка repurchases."
    ),
    "EBITDA": (
        "\n⚠️ EBITDA может быть раскрыта как отдельная строка в сводной таблице (пресс-релиз) или "
        "рассчитываться из EBIT + Амортизация. Ищи явную строку «EBITDA» или «OIBDA». "
        "В сегментной ноте МСФО строка может называться «EBITDA по сегменту»; "
        "для основного показателя бери колонку «Итого» / «Total», а не отдельные "
        "сегменты и не исключения межсегментных операций. Если не раскрыта явно — верни «нет данных»."
    ),
    "EPS": (
        "\n⚠️ Ищи строку «Базовая прибыль на акцию» / «Basic earnings per share». "
        "Не бери «разводнённую» / diluted EPS если есть отдельная basic. "
        "Значение может быть дробным (например 12.34 или 1.01) и иметь единицу USD/share."
    ),
    "COMBINED_ASSET_PURCHASES": (
        "\n⚠️ Ищи строку «Приобретение основных средств и нематериальных активов» — единую строку ДДС. "
        "Если раздельно — верни «нет данных» (это другие показатели)."
    ),
    # ── Bank-specific hints ─────────────────────────────────────────────────
    "TOTAL_LOANS_TO_CLIENTS": (
        "\n⚠️ Ищи строку «Итого кредиты и авансы клиентам» или «Кредиты клиентам» в ОФП. "
        "Нужна сумма ПОСЛЕ вычета резерва ECL (нетто). Если в таблице есть отдельно «Валовые кредиты» и "
        "«Резерв под ожидаемые кредитные убытки (ECL)» — верни нетто-строку «Итого кредиты клиентам»."
    ),
    "CORPORATE_LOANS": (
        "\n⚠️ КРИТИЧНО — БЕРИ GROSS, НЕ NET. Типичная таблица в примечании «Кредиты клиентам»:\n"
        "  | Итого кредиты юр.лицам | 99 293 455 | (9 596 526) | 89 696 929 | 9.7% |\n"
        "  колонки:                     ↑GROSS      ↑ECL        ↑NET       ↑coverage\n"
        "Тебе нужна ПЕРВАЯ колонка (gross), а НЕ третья (net). "
        "Это самая частая ошибка: модель берёт net 89 696 929 вместо gross 99 293 455. "
        "Если видишь 3 числа в строке с заголовками «Валовая | ECL | Нетто» — бери первое."
    ),
    "INDIVIDUALS_LOANS": (
        "\n⚠️ КРИТИЧНО — БЕРИ GROSS, НЕ NET. Та же ловушка что в CORPORATE_LOANS. "
        "Структура строки: GROSS | (ECL allowance) | NET. Тебе нужно ПЕРВОЕ число.\n"
        "Пример: «Итого кредитов физическим лицам 18 114,6 (908,8) 17 205,8» → бери 18 114,6, "
        "а НЕ 17 205,8. Это общая сумма кредитов физлицам (ипотека + потребительские + автокредиты), "
        "валовая до вычета ECL."
    ),
    "LOANS_STAGE1_GROSS": (
        "\n⚠️ ИТОГ валовой балансовой стоимости Stage 1 по ВСЕМУ кредитному портфелю "
        "(corporate + retail + interbank + всё прочее), а не по одному сегменту. "
        "Часто стадии раскрываются ДВАЖДЫ: в примечании «Корпоративные кредиты» (только корп) "
        "и в примечании «Розничные кредиты» (только retail) — НЕ бери эти подитоги. Нужен "
        "сводный итог по всем сегментам кредитов клиентам. Если есть таблица «Все кредиты клиентам "
        "по стадиям ECL» — бери Stage 1 из неё. Если только сегментные — суммируй вручную и укажи "
        "в комментарии «итого по сегментам». НЕ бери значение ECL allowance Stage 1 — это другой показатель."
    ),
    "LOANS_STAGE2_GROSS": (
        "\n⚠️ ИТОГ валовой балансовой стоимости Stage 2 по ВСЕМУ кредитному портфелю "
        "(corporate + retail + всё прочее), не подитог одного сегмента. "
        "Не путай с ECL резервом Stage 2. См. правило для STAGE1_GROSS."
    ),
    "LOANS_STAGE3_GROSS": (
        "\n⚠️ ИТОГ валовой балансовой стоимости Stage 3 по ВСЕМУ кредитному портфелю "
        "(corporate + retail + всё прочее), не подитог одного сегмента. Stage 3 ≈ обесцененные кредиты. "
        "Не путай с ECL резервом Stage 3. См. правило для STAGE1_GROSS."
    ),
    "ECL_ALLOWANCE_STAGE1": (
        "\n⚠️ Строка «Резерв под ожидаемые кредитные убытки» / «ECL allowance», столбец Stage 1. "
        "Обычно показывается как отрицательное число или в скобках — сохрани знак/скобки как в источнике."
    ),
    "ECL_ALLOWANCE_STAGE2": (
        "\n⚠️ Строка «Резерв под ожидаемые кредитные убытки», столбец Stage 2. Сохрани знак/скобки как в источнике."
    ),
    "ECL_ALLOWANCE_STAGE3": (
        "\n⚠️ Строка «Резерв под ожидаемые кредитные убытки», столбец Stage 3. Сохрани знак/скобки как в источнике. "
        "Это самый крупный резерв среди трёх стадий."
    ),
    "SECURITIES_FVPL": (
        "\n⚠️ Ищи в ОФП строку «Финансовые активы, оцениваемые по справедливой стоимости через прибыль или убыток» "
        "(ССПЧПУ / FVPL / FVTPL). Это отдельная строка от FVOCI и amortised cost."
    ),
    "SECURITIES_FVOCI": (
        "\n⚠️ Ищи в ОФП строку «Финансовые активы, оцениваемые по справедливой стоимости через прочий совокупный доход» "
        "(ССПСД / FVOCI). Не путай с FVPL."
    ),
    "SECURITIES_AMORTISED_COST": (
        "\n⚠️ Ищи в ОФП строку «Финансовые активы, оцениваемые по амортизированной стоимости» (AC). "
        "Обычно это долговые ценные бумаги, удерживаемые до погашения."
    ),
    "REPO_PLEDGED_SECURITIES": (
        "\n⚠️ Ищи строку «Ценные бумаги, заложенные по договорам репо» или «Обременённые ценные бумаги». "
        "Если не раскрыто в ОФП — ищи в примечании «Финансовые активы» или «Обременённые активы»."
    ),
    "CASH_AND_CASH_BALANCES_WITH_CB": (
        "\n⚠️ Ищи строку «Денежные средства и остатки в центральном банке» или «Обязательные резервы в ЦБ». "
        "Это ОТДЕЛЬНАЯ строка от «Денежные средства и их эквиваленты» (CASH_EQUIVALENTS)."
    ),
    "FUNDS_FROM_CENTRAL_BANK": (
        "\n⚠️ В пассивах: «Средства центральных банков» / «Кредиты ЦБ» / «Funds from central bank». "
        "Это обязательство (пассив), не актив. Бери ВАЛОВУЮ строку из ОФП.\n"
        "❌ НЕ путать:\n"
        "  • с «Обязательными резервами на счетах ЦБ» (CASH_AND_CASH_BALANCES_WITH_CB, актив)\n"
        "  • с «Субординированными займами от Банка России» (JUNIOR_DEBT — Tier 2 капитал)\n"
        "  • с нетто-разностью / чистой позицией к ЦБ (отдельный показатель)\n"
        "Конкретный пример ошибки на Сбере: модель вернула 349,1 млрд «Субординированные займы от "
        "Банка России» — это JUNIOR_DEBT, не funds from CB. У Сбера funds_from_CB = «нет данных» "
        "(он сам кредитор для ЦБ, а не должник).\n"
        "Если в ОФП нет явной строки «Средства Банка России» / «Funds from CB» — верни «нет данных»."
    ),
    "INTERBANK_DEPOSITS": (
        "\n⚠️ АКТИВ: «Средства в банках» / «Размещения в банках» / «Due from banks». "
        "НЕ путай с «Средства банков» (это пассив INTERBANK_LOANS)."
    ),
    "INTERBANK_LOANS": (
        "\n⚠️ ПАССИВ: «Средства банков» / «Депозиты банков» / «Due to banks». "
        "НЕ путай с «Средства в банках» (это актив INTERBANK_DEPOSITS)."
    ),
    "CLIENT_DEPOSITS": (
        "\n⚠️ Строка «Средства клиентов» / «Депозиты клиентов» в обязательствах. "
        "Нужна ОБЩАЯ сумма (корпоративные + физлица + госорганы). Если в ОФП общая строка — бери её."
    ),
    "CORPORATE_DEPOSITS": (
        "\n⚠️ В примечании «Средства клиентов» ищи разбивку по типам: «Корпоративные клиенты» / «Юридические лица». "
        "Исключи средства физлиц и госорганов."
    ),
    "INDIVIDUALS_DEPOSITS": (
        "\n⚠️ В примечании «Средства клиентов»: «Физические лица». Включает текущие счета и срочные вклады физлиц."
    ),
    "GOVERNMENTAL_DEPOSITS": (
        "\n⚠️ В примечании «Средства клиентов»: «Государственные и муниципальные органы». "
        "Если такой разбивки нет — верни «нет данных»."
    ),
    "DEBT_SECURITIES_ISSUED": (
        "\n⚠️ Пассив: «Выпущенные долговые ценные бумаги» / «Выпущенные облигации». "
        "Нужна общая сумма в обращении (не объём эмиссии за период)."
    ),
    "JUNIOR_DEBT": (
        "\n⚠️ «Субординированные займы» / «Субординированный долг» — отдельная строка в обязательствах. "
        "Суборды учитываются в капитале второго уровня, но в ОФП это обязательство."
    ),
    "SECURITIES_LIABILITIES": (
        "\n⚠️ КРИТИЧНО: ИМЕННО производные / FVPL пассивы.\n"
        "Конкретный поиск в ОФП:\n"
        "  ✓ «Производные финансовые обязательства» — это то что нужно\n"
        "  ✓ «Финансовые обязательства, оцениваемые по справедливой стоимости через прибыль или убыток» (FVPL)\n"
        "  ✗ НЕ «Выпущенные долговые ценные бумаги» / «Выпущенные облигации» — это DEBT_SECURITIES_ISSUED, отдельный показатель!\n"
        "Самая частая ошибка модели на банках: возвращает значение DEBT_SECURITIES_ISSUED "
        "(выпущенные облигации) вместо производных обязательств. Если в ОФП нет ЯВНОЙ строки "
        "с словом «производные» или «FVPL/ССПЧПУ» в пассивах — возвращай «нет данных», не "
        "подставляй выпущенные облигации.\n"
        "Подсказка для проверки: SECURITIES_LIABILITIES обычно МНОГО меньше DEBT_SECURITIES_ISSUED "
        "(деривативы это малая позиция, выпущенные облигации — крупная). Если число совпадает с "
        "выпущенными облигациями — точно перепутал."
    ),
    "INTANGIBLE_ASSETS": (
        "\n⚠️ Балансовая стоимость нематериальных активов БЕЗ гудвилла. У нас гудвил вынесен в "
        "отдельный показатель GOODWILL — НЕ суммируй его сюда. "
        "В таблице НМА бери итог по столбцам «Лицензии + ПО + Прочие НМА» (исключая столбец «Гудвил»). "
        "Если строка в ОФП — «Гудвил и прочие НМА» одной суммой — нужно либо найти разбивку в "
        "примечании и вычесть гудвил, либо вернуть «нет данных»."
    ),
    "OTHER_OPERATING_INCOME": (
        "\n⚠️ В ОПУ банка: блок «Прочие операционные доходы» (в составе операционных доходов, ПОСЛЕ "
        "комиссионных и трейдинга). Состав: доход от выбытия активов, восстановление прочих резервов, "
        "штрафы/пени, доход от выкупленных активов и т. п. "
        "НЕ путай с «Прочими доходами» вне операционного блока — это другой показатель. "
        "Если в ОПУ блок одной строкой «Прочие операционные доходы/(расходы)» нетто — бери эту строку."
    ),
    "DIVIDENDS": (
        "\n⚠️ Дивиденды, ОБЪЯВЛЕННЫЕ за период (банки/НФ). Источник: либо отчёт о движении капитала "
        "(SOCIE — строка «Дивиденды»), либо примечание «Дивиденды». "
        "НЕ путай с дивидендами полученными (DIVIDENDS_RECEIVED) и с дивидендами в ОДДС "
        "(DIVIDENDS_SHAREHOLDERS — выплаченные за период деньгами). "
        "Для банков: знак сохраняем как в источнике (в SOCIE обычно отрицательный — отток из капитала). "
        "Если только «дивиденды на акцию объявленные» (₽/акция) без суммы — верни «нет данных»."
    ),
    "INTEREST_INCOME_ER": (
        "\n⚠️ Строка «Процентные доходы, рассчитанные с использованием метода эффективной процентной ставки» "
        "(не путать с «Прочие процентные доходы»). Обычно первая строка ОПУ банка."
    ),
    "INTEREST_EXPENSE_ER": (
        "\n⚠️ Строка «Процентные расходы, рассчитанные с использованием метода эффективной процентной ставки». "
        "Обычно в скобках. Сохрани знак/скобки как в источнике."
    ),
    "NET_INTEREST_INCOME": (
        "\n⚠️ Строка «Чистый процентный доход» = Процентные доходы − Процентные расходы. "
        "Обычно раскрыта явно в ОПУ. НЕ нужно считать — бери из отчёта."
    ),
    "FEE_AND_COMMISSION_INCOME": (
        "\n⚠️ «Комиссионные доходы» — ВАЛОВАЯ сумма (до вычета комиссионных расходов). "
        "Не путай с «Чистый комиссионный доход» (это разница)."
    ),
    "FEE_AND_COMMISSION_EXPENSE": (
        "\n⚠️ «Комиссионные расходы». Обычно в скобках — сохрани знак/скобки как в источнике."
    ),
    "NET_COMMISSION_INCOME": (
        "\n⚠️ «Чистый комиссионный доход» = комиссионные доходы − комиссионные расходы. "
        "Обычно раскрыт явно как отдельная строка ОПУ."
    ),
    "OPERATING_INCOME_BANKS": (
        "\n⚠️ «Операционные доходы» в ОПУ банка = NII + комиссионный доход + трейдинг + прочие. "
        "Ищи строку «Операционные доходы» / «Total operating income» ДО строки «Операционные расходы»."
    ),
    "TOTAL_CREDIT_ALLOWANCE": (
        "\n⚠️ «Расходы на создание резерва под кредитные убытки» / «Отчисления в резервы» / «ECL charge». "
        "Это расход из ОПУ (не остаток резерва). Обычно в скобках — сохрани знак/скобки как в источнике."
    ),
    "OPERATING_EXPENSES_BANKS": (
        "\n⚠️ ИМЕННО «Операционные расходы» / «Operating expenses» банка — итоговая строка в ОПУ "
        "ПОСЛЕ операционного дохода. Состав: персонал + аренда + амортизация + прочие административные. "
        "НЕ путай с «Непроцентные расходы» / «Non-interest expenses» — это шире (может включать "
        "комиссионные расходы, расходы на резервы, страховые выплаты). Если в ОПУ есть отдельная "
        "строка «Операционные расходы», бери её. Если только «Непроцентные расходы» — НЕ подставляй её, "
        "а верни «нет данных» (это разные показатели, размер обычно отличается на 20-50%). "
        "Обычно в скобках — сохрани знак/скобки как в источнике."
    ),
    "RISK_WEIGHTED_ASSETS": (
        "\n⚠️ «Активы, взвешенные по риску» / «Risk-weighted assets» / «RWA». "
        "Ищи в разделе «Управление капиталом» / «Достаточность капитала»."
    ),
    "COMMON_EQUITY_TIER_ONE_CAPITAL": (
        "\n⚠️ «Основной капитал первого уровня» / «CET1 capital» — числитель в норме CET1. "
        "Ищи в разделе «Достаточность капитала». Не путай с «Tier 1 capital» (может включать AT1)."
    ),
    "CET1_RATIO_DISCLOSED": (
        "\n⚠️ «Норматив CET1» / «CET1 ratio» — уже рассчитанное значение в процентах (например 12.3%). "
        "Бери раскрытое банком значение, НЕ считай сам."
    ),
    "TIER1_RATIO_DISCLOSED": (
        "\n⚠️ «Норматив Tier 1» / «Tier 1 ratio» — в процентах. Бери раскрытое значение."
    ),
    "NON_PERFORMING_LOANS_RATIO": (
        "\n⚠️ «NPL 90+» / «Доля неработающих кредитов» — в процентах (например 3.2%). "
        "Бери раскрытое банком значение из раздела управления кредитным риском."
    ),
    "NON_PERFORMING_LOANS_COVERAGE": (
        "\n⚠️ «Коэффициент покрытия NPL» = резервы / NPL, в процентах. Бери раскрытое значение."
    ),
    "MODIFIED_AUDITORS_OPINION": (
        "\n⚠️ КРИТИЧНО — ВЕРНИ СТРОКУ, НЕ ЧИСЛО! Допустимые значения только:\n"
        "  «Да» — если в аудиторском заключении есть оговорка / отказ от выражения мнения / "
        "негативное мнение / модифицированное мнение\n"
        "  «Нет» — если мнение немодифицированное (без оговорок) / стандартный «clean opinion»\n"
        "  «нет данных» — если аудиторского заключения нет в документе\n"
        "ЗАПРЕЩЕНО: возвращать 0, 1, true, false, числа любого вида. ТОЛЬКО буквальные строки "
        "«Да» / «Нет» / «нет данных».\n"
        "Где искать: первый страницы документа, раздел «Аудиторское заключение независимого аудитора», "
        "обычно есть фраза «Мнение» / «Opinion» — читай её."
    ),
    # ─── Sign-flip hints ────────────────────────────────────────────────────
    # The Лукойл AIFA duplicate-run test (2026-04-29) showed three metrics
    # randomly flipping sign between identical runs (ASSET_PURCHASES,
    # OPERATING_EXPENSES, DIVIDENDS_PAID_NONCONTROLLING_INTERESTS). Cause:
    # the current sign convention preserves source signs. Explicit per-metric reminders
    # reinforce that parentheses/minus must not be stripped.
    "ASSET_PURCHASES": (
        "\n⚠️ ЗНАК: «приобретение основных средств» в инвестиционной деятельности ОДДС — "
        "возвращай ровно как в источнике. "
        "Если в отчёте «(433 042)» / «-433 042» — сохрани скобки/минус. "
        "Не путай с ASSET_SALES_PROCEEDS (поступления от продажи ОС — это другая строка)."
    ),
    "INTANGIBLE_ASSET_PURCHASES": (
        "\n⚠️ ЗНАК: приобретение НМА — отток в ОДДС. Возвращай ровно как в источнике. "
        "В отчёте «(337)» или «-337» → сохрани скобки/минус."
    ),
    "SUBSIDIARY_ACQUISITIONS": (
        "\n⚠️ ЗНАК: приобретение дочерних компаний — отток (M&A). У нас формат: "
        "значение ровно как в источнике. «(10 911)» должно остаться «(10 911)». "
        "Если в отчёте есть и приобретения, и продажи — нужны именно приобретения."
    ),
    "DIVIDENDS_PAID_NONCONTROLLING_INTERESTS": (
        "\n⚠️ ЗНАК: дивиденды миноритариям — отток в финансовой деятельности. "
        "Возвращай ровно как в источнике. "
        "В отчёте «(3 930)» / «-3 930» → сохрани скобки/минус."
    ),
    "DIVIDENDS_SHAREHOLDERS": (
        "\n⚠️ ЗНАК: дивиденды акционерам — отток в финансовой деятельности. "
        "Возвращай ровно как в источнике. «(361 602)» должно остаться «(361 602)». "
        "Бери ИТОГ за период: если в отчёте промежуточные + финальные, суммируй."
    ),
    "SHARES_BUYBACK": (
        # Clarify that 0 ≠ "нет данных"; preserve source sign.
        "\n⚠️ Выкуп собственных акций — отток денег в разделе финансовой деятельности ОДДС. "
        "ЗНАК: верни как в источнике. «(8 271)» / «-8 271» — сохрани скобки/минус. "
        "Если выкупа не было — верни «нет данных», а не 0."
    ),
    # ─── Lease-liability slot mismatch ─────────────────────────────────────
    # Лукойл AIFA test also showed model swapping LONG vs SHORT term lease
    # values between runs (29283 ↔ 185843). Distinct hints anchor each.
    "LONG_TERM_LEASE_LIABILITIES": (
        "\n⚠️ Это NON-CURRENT (долгосрочная) часть обязательств по аренде — то, что "
        "погашается ПОСЛЕ 12 месяцев. На балансе это строка «Обязательства по аренде» "
        "в разделе «Долгосрочные обязательства» (НЕ в «Краткосрочные»). "
        "Если таблица: «Lease liabilities total: 9 034 → Less current portion: (1 266) "
        "→ Non-current: 7 768» — верни 7 768 (НЕ 9 034!). Gross total — это другой "
        "показатель, не путай. По размеру обычно больше short-term, но если аренда "
        "близка к погашению, может быть и меньше — главное это NON-CURRENT строка."
    ),
    "SHORT_TERM_LEASE_LIABILITIES": (
        "\n⚠️ Это CURRENT (текущая) часть обязательств по аренде — то, что погашается "
        "В ТЕЧЕНИЕ 12 месяцев. На балансе это строка «Текущая часть обязательств по "
        "аренде» / «Current portion of lease liabilities» в разделе «Краткосрочные "
        "обязательства». Если таблица: «Lease liabilities total: 9 034 → Less current "
        "portion: (1 266)» — верни значение как в источнике, не удаляй скобки. НЕ путай с "
        "LONG_TERM_LEASE_LIABILITIES (non-current)."
    ),
}

# Cost/outflow metric codes are still useful for prompt emphasis, but signs are
# now preserved verbatim: parentheses/minus in the source mean a negative value.
_COST_METRIC_CODES = {m["enum"] for m in EXTRACTABLE_NON_FINANCIAL + EXTRACTABLE_BANK if m.get("is_cost")}

# Registry doesn't yet carry an `is_cost` flag for most outflow metrics, so the
# list below remains as a semantic hint only. Do not use it to take abs(value).
_OUTFLOW_METRIC_CODES: set[str] = {
    # Operating costs / expenses
    "COSTS_EXPENSES", "OPERATING_EXPENSES", "OPERATING_EXPENSES_BANKS",
    "COST_OF_SALES", "FINANCIAL_EXPENSES", "FEE_AND_COMMISSION_EXPENSE",
    "DEPRECIATION_AMORTIZATION", "INCOME_TAX",
    # Cash-flow outflows (paid)
    "INTEREST_PAID", "ASSET_PURCHASES", "CAPEX", "DIVIDENDS",
    "DIVIDENDS_SHAREHOLDERS", "DIVIDENDS_PAID", "SHARES_BUYBACK",
    "PRINCIPAL_REPAYMENT_OF_LEASE_LIABILITIES",
    "INTANGIBLE_ASSET_PURCHASES", "SUBSIDIARY_ACQUISITIONS",
    # Balance-sheet liabilities can appear in parentheses in US statements.
    "SHORT_TERM_LEASE_LIABILITIES", "LONG_TERM_LEASE_LIABILITIES",
    # Provisions / allowances (positive magnitudes per registry)
    "TOTAL_CREDIT_ALLOWANCE", "DOUBTFUL_ACCOUNTS_ALLOWANCE",
    "CHANGE_IN_EXPECTED_CREDIT_LOSSES", "ECL_ALLOWANCE_STAGE1",
    "ECL_ALLOWANCE_STAGE2", "ECL_ALLOWANCE_STAGE3",
    "INSURANCE_EXPENSE", "NET_OTHER_RESERVES",
}
_COST_METRIC_CODES = _COST_METRIC_CODES | _OUTFLOW_METRIC_CODES


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _detect_company_type_from_chunks(chunks) -> str:
    """Wrap the text-based detector for the legacy RAG path."""
    text = " ".join(getattr(c, "content", "") for c in chunks[:30])
    return _detect_company_type(text)


def _detect_unit(chunks) -> str:
    text = " ".join(getattr(c, "content", "") for c in chunks[:10]).lower()
    for unit in ["млрд руб", "млн долл", "млн евр", "тыс. руб", "тыс руб", "млн руб", "в миллионах"]:
        if unit in text:
            return unit
    return ""


def _detect_periods(chunks) -> tuple[str, str]:
    text = " ".join(getattr(c, "content", "") for c in chunks[:10])
    years = sorted(set(re.findall(r'\b(20\d{2})\b', text)), reverse=True)
    if len(years) >= 2:
        return years[0], years[1]
    elif years:
        return years[0], str(int(years[0]) - 1)
    return "Текущий", "Предыдущий"


def _safe_num(v) -> Optional[float]:
    if v is None:
        return None
    raw = str(v).strip()
    parsed = _parse_verbatim_value(raw)
    if parsed is not None:
        return parsed
    # Accounting negative: (1 234)
    m = re.match(r'^\(([0-9][0-9 \u00a0]*)\)$', raw)
    if m:
        try:
            return -float(m.group(1).replace(" ", "").replace("\u00a0", ""))
        except ValueError:
            pass
    # Remove non-breaking spaces, normalize
    clean = raw.replace("\u00a0", " ").replace(",", ".")
    m2 = re.search(r'-?(\d[\d ]*\d|\d)(?:\.\d+)?', clean)
    if m2:
        try:
            return float(m2.group().replace(" ", ""))
        except ValueError:
            pass
    return None


def _normalize_period_calendar_label(
    period: str,
    period_calendar: str,
    *,
    value_kind: str | None = None,
    already_normalized: bool = False,
) -> str:
    if _is_indian_fiscal_calendar(period_calendar):
        return normalize_indian_fiscal_period_label(
            period,
            value_kind=value_kind,
            already_normalized=already_normalized,
        )
    return normalize_period_label(period, value_kind=value_kind)


def _is_indian_fiscal_calendar(period_calendar: str) -> bool:
    return str(period_calendar or "").strip().lower().replace("-", "_") == "indian_fiscal"


def _normalize_period_calendar_labels(periods: list[str], period_calendar: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for period in periods:
        norm = _normalize_period_calendar_label(period, period_calendar)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _indian_annual_report_fiscal_year(text: str) -> int | None:
    src = str(text or "")[:120_000].replace("\u00a0", " ")
    m = re.search(
        r"\bannual\s+report\s+(?P<start>20\d{2})\s*[-–/]\s*(?P<end>\d{2}|20\d{2})\b",
        src,
        re.I,
    )
    if not m:
        return None
    start_year = int(m.group("start"))
    end_raw = m.group("end")
    if len(end_raw) == 4:
        return start_year
    end_year = (start_year // 100) * 100 + int(end_raw)
    if end_year < start_year:
        end_year += 100
    return start_year if end_year in {start_year, start_year + 1} else end_year


def _prefer_indian_annual_report_periods(pdf_text: str, periods: list[str]) -> list[str]:
    report_year = _indian_annual_report_fiscal_year(pdf_text)
    if not report_year:
        return periods
    wanted = [f"FY {year}" for year in range(report_year, report_year - 5, -1)]
    annuals = [period for period in wanted if period in periods]
    return annuals or periods


def _parse_verbatim_value(text) -> Optional[float]:
    """Parse a verbatim cell text from PDF, RU/US locale aware.

    Used by the new verbatim-transcript extraction path (E-1 fix, 2026-05-05):
    LLM returns the cell text as-written ("2 999,6", "(442,8)", "—") and this
    function does deterministic numeric parsing — no LLM arithmetic involved.

      "2 999,6"   → 2999.6     (RU: space=thousands, comma=decimal)
      "60 855,1"  → 60855.1
      "(442,8)"   → -442.8     (parentheses = accounting negative)
      "1,234.56"  → 1234.56    (US fallback: comma=thousand, period=decimal)
      "—" "–" "-" "Н/Д" "" → None
      None / non-string → None
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip()
    if not s:
        return None
    # Sentinels for "no data"
    if s in ("—", "–", "−", "-", "Н/Д", "н/д", "n/a", "N/A", "NA", "null"):
        return None
    s = s.replace("\u00a0", " ").replace("\u2009", " ").replace("\u202f", " ")
    # Strip wrapping quotes / guillemets / footnote markers / trailing %
    s = s.strip("«»‹›\"'\u201c\u201d\u2018\u2019")
    s = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]", "", s)
    s = s.rstrip("%").strip()
    is_neg = False
    # Currency symbols/codes can be part of a verbatim table cell in US GAAP
    # reports ("$ 25,785", "USD 1,234"). They identify unit/currency, not
    # numeric scale, so strip them before deterministic numeric parsing.
    s = re.sub(r"^(?:[$€£¥₹₽]|USD|US\$|EUR|GBP|RUB|INR|RS\.?)\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*(?:[$€£¥₹₽]|USD|US\$|EUR|GBP|RUB|INR|RS\.?)$", "", s, flags=re.IGNORECASE)
    # Parens = negative after currency stripping. Supports cells like "$ (0.01)".
    if s.startswith("(") and s.endswith(")"):
        is_neg = True
        s = s[1:-1].strip()
    # Optional explicit minus prefix, possibly after currency stripping.
    if s.startswith("-"):
        is_neg = not is_neg
        s = s[1:].strip()
    s = re.sub(r"^(?:[$€£¥₹₽]|USD|US\$|EUR|GBP|RUB|INR|RS\.?)\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*(?:[$€£¥₹₽]|USD|US\$|EUR|GBP|RUB|INR|RS\.?)$", "", s, flags=re.IGNORECASE)
    # Heuristic: RU locale (the common case in our corpus) — space is
    # thousand separator, comma is decimal. Strip spaces then comma→period.
    s_no_space = s.replace(" ", "")
    if not s_no_space:
        return None
    comma_count = s_no_space.count(",")
    period_count = s_no_space.count(".")
    if comma_count > 0 and period_count > 0:
        # US locale ("1,234.56") — strip commas
        s_no_space = s_no_space.replace(",", "")
    elif comma_count == 1:
        head, tail = s_no_space.split(",", 1)
        # Treat as decimal when:
        #  - head starts with 0 ("0,1612" = ratio) — always decimal
        #  - tail length 1-2 and head is numeric — RU decimal
        #  - tail length 3 and head is numeric — US thousands ("42,245")
        # Real US thousands ("1,234,567") have multiple commas → caught below.
        if head in ("", "0") or head.startswith("0"):
            s_no_space = head + "." + tail
        elif 1 <= len(tail) <= 2 and tail.isdigit() and head.replace(".", "").isdigit():
            s_no_space = head + "." + tail
        else:
            s_no_space = s_no_space.replace(",", "")
    elif comma_count > 1:
        # Multiple commas — US thousands grouping
        s_no_space = s_no_space.replace(",", "")
    # Now s_no_space should be a clean number with optional period
    try:
        n = float(s_no_space)
    except ValueError:
        return None
    return -n if is_neg else n


def _currency_from_unit_text(text: object) -> str:
    s = str(text or "").lower()
    if "$" in s or "usd" in s or "dollar" in s or "долл" in s:
        return "USD"
    if "€" in s or "eur" in s or "euro" in s or "евро" in s:
        return "EUR"
    if "₹" in s or "inr" in s or "rupee" in s or re.search(r"\brs\.?\b", s):
        return "INR"
    if "руб" in s or "rouble" in s or "ruble" in s or "₽" in s or "rub" in s:
        return "руб."
    return ""


def _default_unit_for_standard(standard: str) -> str:
    if standard in {"ifrs_en", "us_gaap_en"}:
        return "млн. $"
    if standard in {"ind_as_en", "ind_gaap_en"}:
        return "INR"
    return "млн. руб."


def _canonical_money_unit(scale: str, currency: str) -> str:
    if currency == "руб.":
        return {"трлн": "трлн руб.", "млрд": "млрд руб.", "млн": "млн. руб.", "тыс": "тыс. руб.", "one": "руб."}[scale]
    if currency == "USD":
        return {"трлн": "трлн $", "млрд": "млрд $", "млн": "млн. $", "тыс": "тыс. $", "one": "$"}[scale]
    if currency == "INR":
        return {"трлн": "трлн INR", "млрд": "млрд INR", "млн": "млн INR", "тыс": "тыс INR", "one": "INR"}[scale]
    if currency:
        return {"трлн": f"трлн {currency}", "млрд": f"млрд {currency}", "млн": f"млн {currency}", "тыс": f"тыс {currency}", "one": currency}[scale]
    return {"трлн": "трлн", "млрд": "млрд", "млн": "млн", "тыс": "тыс", "one": ""}[scale]


def _parse_unit_text(text, default_currency: str = "") -> str:
    """Map verbatim unit text from PDF to canonical unit label.

    Used by the verbatim extraction path. Returns one of:
      "млрд руб.", "млн. руб.", "тыс. руб.", "руб.", "млн INR",
      "crore INR", "lakh INR", "INR", "%", or "" (unknown).
    Downstream `_normalise_money_value` converts money scales to millions
    while preserving the detected currency where possible.
    """
    if not text:
        return ""
    s = str(text).lower().strip()
    if not s:
        return ""
    # % / ratios
    if "%" in s or "коэффициент" in s or "коэф." in s or "ratio" in s or "доля" in s:
        return "%"
    currency = _currency_from_unit_text(s) or default_currency
    # Order matters: млрд before млн (substring), миллиард before миллион
    if (
        "млрд" in s
        or "миллиард" in s
        or "billion" in s
        or re.search(r"(?<![a-z])bn\.?(?![a-z])", s)
        or "трлн" in s
        or "триллион" in s
        or re.search(r"(?<![a-z])tn\.?(?![a-z])", s)
    ):
        # Treat трлн = 1000 × млрд by mapping to млрд (factor in _MONEY_SCALE_FACTORS handles it)
        return _canonical_money_unit("трлн", currency) if (
            "трлн" in s or "триллион" in s or re.search(r"(?<![a-z])tn\.?(?![a-z])", s)
        ) else _canonical_money_unit("млрд", currency)
    if "arab" in s or "arabs" in s:
        return f"arab {currency or 'INR'}".strip()
    if "crore" in s or "crores" in s or re.search(r"(?<![a-z])cr\.?(?![a-z])", s):
        return f"crore {currency or 'INR'}".strip()
    if "lakh" in s or "lakhs" in s or "lac" in s or "lacs" in s:
        return f"lakh {currency or 'INR'}".strip()
    if "тыс" in s or "thous" in s or re.search(r"(?<![a-z])th\.?(?![a-z])", s):
        return _canonical_money_unit("тыс", currency)
    if (
        "млн" in s
        or "миллион" in s
        or "million" in s
        or re.search(r"(?<![a-z])m(?:n|ln)?\.?(?![a-z])", s)
    ):
        return _canonical_money_unit("млн", currency)
    if "₹" in s or "inr" in s or "rupee" in s or re.search(r"\brs\.?\b", s):
        return "INR"
    if "руб" in s or "rouble" in s or "ruble" in s or "₽" in s or "rub" in s:
        return "руб."
    if "$" in s or "usd" in s or "dollar" in s or "долл" in s:
        return "USD"
    if "€" in s or "eur" in s or "euro" in s or "евро" in s:
        return "EUR"
    return ""


def _normalise_eps_unit(unit_text: object, default_unit: str = "") -> str:
    """EPS is per-share even when the table header only says rubles/kopecks."""
    s = str(unit_text or default_unit or "").lower().strip()
    if "коп" in s or "kopeck" in s or "kopey" in s:
        return "коп./акция"
    if "$" in s or "usd" in s or "dollar" in s or "долл" in s:
        return "$/share"
    if "€" in s or "eur" in s or "euro" in s or "евро" in s:
        return "EUR/share"
    if "₹" in s or "inr" in s or "rupee" in s or re.search(r"\brs\.?\b", s):
        return "INR/share"
    if "руб" in s or "₽" in s or "rub" in s or "rouble" in s or "ruble" in s:
        return "руб/акция"
    return "руб/акция"


def _pick_two_values(raw: str) -> tuple[Optional[float], Optional[float]]:
    """Backwards-compatible thin wrapper that drops the unit. Use
    `_pick_values_and_unit` when you also want the per-metric unit string."""
    cur, prev, _ = _pick_values_and_unit(raw)
    return cur, prev


def _pick_values_and_unit(
    raw: str,
) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Parse the model's structured response.

    Expected format (post-2026-05 prompt):
        текущий: <число>
        предыдущий: <число>
        единица: <шкала>

    The unit line is optional — older prompts didn't ask for it. When
    absent we return None and let the caller fall back to the global
    `_detect_unit(chunks)` heuristic.

    Falls back to scanning the response for the last two numbers if
    structured format is absent (legacy / malformed responses).
    """
    text = raw.strip()
    current = prev = None
    unit: Optional[str] = None

    for line in text.split("\n"):
        stripped = line.strip().lower()
        if stripped.startswith("текущий:") or stripped.startswith("current:"):
            val_str = re.split(r':\s*', line, 1)[-1].strip()
            if not any(kw in val_str.lower() for kw in ("не найдено", "not found", "нет данных", "n/a")):
                current = _safe_num(val_str)
        elif stripped.startswith("предыдущий:") or stripped.startswith("previous:"):
            val_str = re.split(r':\s*', line, 1)[-1].strip()
            if not any(kw in val_str.lower() for kw in ("не найдено", "not found", "нет данных", "n/a")):
                prev = _safe_num(val_str)
        elif stripped.startswith("единица:") or stripped.startswith("unit:"):
            u = re.split(r':\s*', line, 1)[-1].strip()
            if u and u.lower() not in ("не найдено", "not found", "нет данных", "n/a", "—", "-"):
                # Strip surrounding quotes/brackets the model sometimes adds.
                u = u.strip('«»"\'<> []').strip()
                unit = u or None

    # Fallback: extract last two distinct numbers from response
    if current is None and prev is None:
        # Remove years (4-digit 20xx) to avoid confusing them with values
        no_years = re.sub(r'\b20\d{2}\b', '', text)
        nums = re.findall(r'-?\d[\d\s]*\d(?:\.\d+)?|-?\d(?:\.\d+)?', no_years)
        nums = [n.strip() for n in nums if len(n.strip().replace(" ", "")) >= 1]
        clean_nums = []
        for n in nums[-6:]:
            v = _safe_num(n)
            if v is not None:
                clean_nums.append(v)
        if len(clean_nums) >= 2:
            current, prev = clean_nums[-2], clean_nums[-1]
        elif len(clean_nums) == 1:
            current = clean_nums[-1]

    return current, prev, unit


# ── Unit normalisation ─────────────────────────────────────────────────
# All consumers (chains, quick_synthesis, calculated metrics, the metrics
# table in the UI) are simpler when every value is in the same scale.
# We pick `млн руб.` as canonical: human-readable for big banks/corps and
# avoids float precision issues (millions of millions kept tidy).
#
# When the model reports "млрд руб." (billions) we multiply by 1000.
# "тыс. руб." → divide by 1000. Anything we can't classify as a money
# scale (%, шт., тонн, etc.) is passed through unchanged — we don't try
# to convert non-money units, and the unit string is preserved so the UI
# can label the column correctly.
_MONEY_SCALE_FACTORS: dict[str, float] = {
    "трлн": 1_000_000.0,  # rare but possible
    "arab": 1000.0,
    "arabs": 1000.0,
    "crore": 10.0,
    "crores": 10.0,
    "lakh": 0.1,
    "lakhs": 0.1,
    "lac": 0.1,
    "lacs": 0.1,
    "миллиард": 1000.0,
    "млрд": 1000.0,
    "billion": 1000.0,
    "млн": 1.0,
    "миллион": 1.0,
    "million": 1.0,
    "тыс": 0.001,
    "thousand": 0.001,
}


def _normalise_money_value(
    value: Optional[float],
    unit: Optional[str],
) -> tuple[Optional[float], str]:
    """Convert (value, unit) into million-scale money while preserving currency.

    If `unit` is empty / not money (e.g. "%"), passes through.
    Currency-only Indian units such as "INR" or "Rs." are treated as one
    rupee and converted to millions with a 1e-6 factor. Existing RUB/USD
    currency-only behavior is preserved.
    Returns the canonical unit label so the metric record is consistent.
    """
    if value is None:
        return None, (unit or "")
    if not unit:
        return value, ""
    u = unit.lower().strip()
    # Non-money: %, шт., тонн, дней, … — leave as-is, propagate unit.
    if not any(kw in u for kw in (
        "arab", "arabs",
        "crore", "crores", "lakh", "lakhs", "lac", "lacs",
        "трлн", "миллиард", "млрд", "billion",
        "млн", "миллион", "million",
        "тыс", "thousand",
        "₹", "inr", "rupee", "rs.",
        "руб", "₽", "rub",
    )):
        return value, unit
    factor: float | None = None
    for kw, f in _MONEY_SCALE_FACTORS.items():
        if kw in u:
            factor = f
            break
    currency = _currency_from_unit_text(unit)
    if factor is None:
        factor = 0.000001 if currency == "INR" else 1.0
    return value * factor, _canonical_money_unit("млн", currency)


def _select_run_periods(
    pdf_text: str,
    doc_period: str = "",
    fallback_period_label: str = "Текущий",
    period_calendar: str = "calendar",
) -> tuple[list[str], str, str]:
    """Pick normalized periods for the run while keeping legacy current/prev.

    `fallback_period_label` is shown to the user when no periods can be
    detected at all (rare edge case). Cells override this to "Current"
    for english-language reports so the UI doesn't display Russian text.
    """
    fiscal_context = FiscalPeriodContext.from_text(pdf_text)
    detected = detect_period_candidates(pdf_text, fiscal_context=fiscal_context)
    doc_norm = normalize_period_label(doc_period, fiscal_context=fiscal_context) if doc_period else ""
    periods = _filter_plausible_report_periods(sort_periods(([doc_norm] if doc_norm else []) + detected))
    if not periods:
        year = _detect_year_from_text(pdf_text) or ""
        if year:
            periods = [normalize_period_label(year, fiscal_context=fiscal_context)]
    periods = _prefer_fiscal_context_annual_periods(periods, fiscal_context)
    periods = _dedupe_equivalent_report_periods(
        _prefer_financial_report_periods(periods),
        period_calendar,
    )
    periods = _normalize_period_calendar_labels(periods, period_calendar)
    periods = _dedupe_equivalent_report_periods(sort_periods(periods), period_calendar)
    if _is_indian_fiscal_calendar(period_calendar):
        periods = _drop_indian_non_march_rolling_annual_noise(periods)
        periods = _prefer_indian_annual_report_periods(pdf_text, periods)
    periods = _drop_same_year_annual_periods_for_interim_reports(periods)
    period_current = periods[0] if periods else (doc_norm or fallback_period_label)
    period_prev = _select_previous_run_period(period_current, periods, period_calendar)
    return periods, period_current, period_prev


def _prefer_fiscal_context_annual_periods(
    periods: list[str],
    fiscal_context: FiscalPeriodContext,
) -> list[str]:
    """Drop false annual ended-date candidates when the report defines fiscal years."""
    if not fiscal_context.has_shifted_years():
        return periods
    out: list[str] = []
    for period in periods:
        norm = normalize_period_label(period)
        if m := re.fullmatch(r"12M (\d{1,2})\.(\d{2})\.(20\d{2})", norm):
            day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            expected = fiscal_context.end_date_for_year(year)
            if expected and (day, month, year) != (expected.day, expected.month, expected.year):
                continue
        out.append(norm)
    return out or periods


def _is_preferred_report_period(label: object) -> bool:
    period = normalize_period_label(label)
    if re.fullmatch(r"FY 20\d{2}", period):
        return True
    if re.fullmatch(r"Q[1-4] 20\d{2}", period):
        return True
    if re.fullmatch(r"(?:[369]|12)M (?:20\d{2}|\d{1,2}\.\d{2}\.20\d{2})", period):
        return True
    if m := re.fullmatch(r"(\d{1,2})\.(\d{2})\.(20\d{2})", period):
        day = int(m.group(1))
        month = int(m.group(2))
        return (month, day) in {(3, 31), (6, 30), (9, 30), (12, 31)}
    return False


def _prefer_financial_report_periods(periods: list[str]) -> list[str]:
    preferred = [period for period in periods if _is_preferred_report_period(period)]
    return preferred or periods


def _indian_fiscal_period_end_key(period: object) -> tuple[int, int, int] | None:
    """Actual statement date behind an Indian fiscal period label."""
    p = normalize_period_label(period)
    point_key = _point_period_key(p)
    if point_key is not None:
        return point_key
    if m := re.fullmatch(r"FY (20\d{2})", p):
        return int(m.group(1)) + 1, 3, 31
    if m := re.fullmatch(r"Q([1-4]) (20\d{2})", p):
        quarter = int(m.group(1))
        start_year = int(m.group(2))
        return {
            1: (start_year, 6, 30),
            2: (start_year, 9, 30),
            3: (start_year, 12, 31),
            4: (start_year + 1, 3, 31),
        }[quarter]
    if m := re.fullmatch(r"([69])M (20\d{2})", p):
        months = int(m.group(1))
        start_year = int(m.group(2))
        month_day = {6: (9, 30), 9: (12, 31)}[months]
        return start_year, month_day[0], month_day[1]
    if m := re.fullmatch(r"12M (20\d{2})", p):
        return int(m.group(1)) + 1, 3, 31
    if m := re.fullmatch(r"\d{1,2}M (\d{1,2})[.](\d{1,2})[.](20\d{2})", p):
        day, month, year = map(int, m.groups())
        return year, month, day
    return None


def _report_column_key(label: object, period_calendar: str = "calendar") -> tuple[str, int, int] | None:
    if _is_indian_fiscal_calendar(period_calendar):
        period = normalize_period_label(label)
        if m := re.fullmatch(r"FY (20\d{2})", period):
            return ("annual", int(m.group(1)), 0)
        if m := re.fullmatch(r"Q([1-4]) (20\d{2})", period):
            return ("quarter", int(m.group(2)), int(m.group(1)))
        if m := re.fullmatch(r"([69])M (20\d{2})", period):
            return ("months", int(m.group(2)), int(m.group(1)))
        if m := re.fullmatch(r"(\d{1,2})M (\d{1,2})[.](\d{1,2})[.](20\d{2})", period):
            months, day, month, year = map(int, m.groups())
            fiscal_year = year - 1 if month <= 3 else year
            if months == 12 and (month, day) == (3, 31):
                return ("annual", fiscal_year, 0)
            if months == 9 and (month, day) == (12, 31):
                return ("months", fiscal_year, 9)
            if months == 6 and (month, day) == (9, 30):
                return ("months", fiscal_year, 6)
            if months == 3:
                quarter = {(6, 30): 1, (9, 30): 2, (12, 31): 3, (3, 31): 4}.get((month, day))
                if quarter:
                    return ("quarter", fiscal_year, quarter)
            return ("rolling", year * 10_000 + month * 100 + day, months)
        end_key = _indian_fiscal_period_end_key(period)
        if end_key is not None:
            year, month, day = end_key
            fiscal_year = year - 1 if month <= 3 else year
            if (month, day) == (3, 31):
                return ("annual", fiscal_year, 0)
            quarter = {(6, 30): 1, (9, 30): 2, (12, 31): 3}.get((month, day))
            if quarter:
                return ("quarter", fiscal_year, quarter)
        return None

    period = normalize_period_label(label)
    if m := re.fullmatch(r"FY (20\d{2})", period):
        return ("annual", int(m.group(1)), 0)
    if m := re.fullmatch(r"12M (?:31\.12\.)?(20\d{2})", period):
        return ("annual", int(m.group(1)), 0)
    if m := re.fullmatch(r"Q([1-4]) (20\d{2})", period):
        return ("quarter", int(m.group(2)), int(m.group(1)))
    if m := re.fullmatch(r"(\d{1,2})\.(\d{2})\.(20\d{2})", period):
        day = int(m.group(1))
        month = int(m.group(2))
        year = int(m.group(3))
        if month == 12 and day == 31:
            return ("annual", year, 0)
        quarter = {(3, 31): 1, (6, 30): 2, (9, 30): 3}.get((month, day))
        if quarter:
            return ("quarter", year, quarter)
    return None


def _dedupe_equivalent_report_periods(periods: list[str], period_calendar: str = "calendar") -> list[str]:
    out: list[str] = []
    seen_report_columns: set[tuple[str, int, int]] = set()
    seen_labels: set[str] = set()
    report_index: dict[tuple[str, int, int], int] = {}
    report_priorities: dict[tuple[str, int, int], int] = {}
    for period in periods:
        norm = normalize_period_label(period)
        if not norm or norm in seen_labels:
            continue
        report_key = _report_column_key(norm, period_calendar)
        if report_key is not None:
            if report_key in seen_report_columns:
                priority = _report_period_priority(norm, period_calendar)
                if priority <= report_priorities.get(report_key, 0):
                    continue
                old_index = report_index[report_key]
                seen_labels.discard(out[old_index])
                out[old_index] = norm
                seen_labels.add(norm)
                report_priorities[report_key] = priority
                continue
            seen_report_columns.add(report_key)
            report_index[report_key] = len(out)
            report_priorities[report_key] = _report_period_priority(norm, period_calendar)
        seen_labels.add(norm)
        out.append(norm)
    return out


def _report_period_priority(period: str, period_calendar: str = "calendar") -> int:
    norm = normalize_period_label(period)
    if _is_indian_fiscal_calendar(period_calendar):
        if re.fullmatch(r"\d{1,2}[.]\d{1,2}[.]20\d{2}", norm):
            return 0
        if re.fullmatch(r"12M \d{1,2}[.]\d{1,2}[.]20\d{2}", norm):
            return 1
        if re.fullmatch(r"(?:FY|Q[1-4]|[69]M) 20\d{2}", norm):
            return 3
        return 2
    return 1


def _drop_indian_non_march_rolling_annual_noise(periods: list[str]) -> list[str]:
    named_end_keys: set[tuple[int, int, int]] = set()
    for period in periods:
        norm = normalize_period_label(period)
        if re.fullmatch(r"12M \d{1,2}[.]\d{1,2}[.]20\d{2}", norm):
            continue
        end_key = _indian_fiscal_period_end_key(norm)
        if end_key is not None:
            named_end_keys.add(end_key)

    if not named_end_keys:
        return periods

    out: list[str] = []
    for period in periods:
        norm = normalize_period_label(period)
        if m := re.fullmatch(r"12M (\d{1,2})[.](\d{1,2})[.](20\d{2})", norm):
            day, month, year = map(int, m.groups())
            if (month, day) != (3, 31) and (year, month, day) in named_end_keys:
                continue
        out.append(period)
    return out


def _select_previous_run_period(
    period_current: str,
    periods: list[str],
    period_calendar: str = "calendar",
) -> str:
    comparable_prev = previous_period_label(period_current)
    if comparable_prev in periods:
        return comparable_prev

    comparable_key = _report_column_key(comparable_prev, period_calendar)
    if comparable_key is not None:
        for period in periods:
            if _report_column_key(period, period_calendar) == comparable_key:
                return period
        return comparable_prev

    current_key = _report_column_key(period_current, period_calendar)
    for period in periods[1:]:
        if current_key is None or _report_column_key(period, period_calendar) != current_key:
            return period
    return comparable_prev


def _interim_quarter_for_run_period(label: object) -> int | None:
    period = normalize_period_label(label)
    if m := re.fullmatch(r"Q([1-4]) 20\d{2}", period):
        return int(m.group(1))
    if m := re.fullmatch(r"([369])M 20\d{2}", period):
        return int(m.group(1)) // 3
    if m := re.fullmatch(r"(\d{1,2})M \d{1,2}\.(\d{2})\.20\d{2}", period):
        months = int(m.group(1))
        month = int(m.group(2))
        if months == 3 and month in {3, 6, 9, 12}:
            return month // 3
    return None


def _drop_same_year_annual_periods_for_interim_reports(periods: list[str]) -> list[str]:
    """Drop same-year FY notes when a Q1-Q3 report period is present.

    Interim reports often quote IAS 34 language such as "not indicative of the
    full year ending December 31, 2026". The broad text scanner sees that as
    FY 2026, but it is not an actual statement column. Keep Q4+FY because some
    annual materials legitimately expose both Q4 and full-year columns.
    """
    pre_q4_interim_years = {
        year
        for period in periods
        for year in [_period_year(period)]
        for quarter in [_interim_quarter_for_run_period(period)]
        if year is not None and quarter is not None and quarter < 4
    }
    if not pre_q4_interim_years:
        return periods

    filtered = [
        period
        for period in periods
        if _annual_anchor_year(period) not in pre_q4_interim_years
    ]
    return filtered or periods


def _period_year(label: object) -> int | None:
    period = normalize_period_label(label)
    m = re.search(r"(20\d{2})", period)
    return int(m.group(1)) if m else None


def _duration_anchor_year(label: object) -> int | None:
    period = normalize_period_label(label)
    if re.fullmatch(r"Q[1-4] 20\d{2}", period):
        return _period_year(period)
    if re.fullmatch(r"[69]M 20\d{2}", period):
        return _period_year(period)
    if re.fullmatch(r"\d{1,2}M \d{1,2}\.\d{2}\.20\d{2}", period):
        return _period_year(period)
    return None


def _annual_anchor_year(label: object) -> int | None:
    period = normalize_period_label(label)
    if re.fullmatch(r"FY 20\d{2}", period):
        return _period_year(period)
    if re.fullmatch(r"12M (?:20\d{2}|\d{1,2}\.\d{2}\.20\d{2})", period):
        return _period_year(period)
    return None


def _date_anchor_year(label: object) -> int | None:
    period = normalize_period_label(label)
    return _period_year(period) if re.fullmatch(r"\d{1,2}\.\d{2}\.20\d{2}", period) else None


def _is_anchor_report_period(label: object) -> bool:
    """Periods tied to actual statement columns, not maturity/future FY notes."""
    period = normalize_period_label(label)
    return bool(
        re.fullmatch(r"Q[1-4] 20\d{2}", period)
        or re.fullmatch(r"[69]M 20\d{2}", period)
        or re.fullmatch(r"\d{1,2}M \d{1,2}\.\d{2}\.20\d{2}", period)
        or re.fullmatch(r"\d{1,2}\.\d{2}\.20\d{2}", period)
    )


def _max_report_anchor_year(periods: list[str]) -> int | None:
    duration_years = [year for p in periods for year in [_duration_anchor_year(p)] if year is not None]
    if duration_years:
        return max(duration_years)

    date_years = [year for p in periods for year in [_date_anchor_year(p)] if year is not None]
    annual_years = [year for p in periods for year in [_annual_anchor_year(p)] if year is not None]
    if annual_years:
        matched_annual_years = [year for year in annual_years if year in date_years]
        if matched_annual_years:
            return max(matched_annual_years)
        return max(annual_years)
    return max(date_years) if date_years else None


def _max_report_anchor_year_for_calendar(periods: list[str], period_calendar: str = "calendar") -> int | None:
    if _is_indian_fiscal_calendar(period_calendar):
        end_years = [
            key[0]
            for period in periods
            for key in [_indian_fiscal_period_end_key(period)]
            if key is not None
        ]
        if end_years:
            return max(end_years)
    return _max_report_anchor_year(periods)


def _filter_plausible_report_periods(periods: list[str], *, max_report_year: int | None = None) -> list[str]:
    """Drop future years that come from notes/maturity schedules, not statements.

    `detect_period_candidates()` scans broad report text, so an interim report
    can contain future years like 2027 in debt repayment schedules. Those years
    must not become `period_current` or UI columns.
    """
    ordered = sort_periods(periods)
    anchor_year = max_report_year or _max_report_anchor_year(ordered)
    if anchor_year is None:
        return ordered

    filtered: list[str] = []
    for p in ordered:
        year = _period_year(p)
        if year is not None and year > anchor_year:
            continue
        filtered.append(p)
    return filtered or ordered


def _select_display_periods(
    periods: list[str],
    fallback_current: str,
    fallback_prev: str,
    period_calendar: str = "calendar",
) -> tuple[str, str]:
    """Pick legacy current/previous labels from final extracted periods.

    Full-context extraction may discover periods that the pre-LLM text
    heuristic missed. The public source of truth is still period_values, but
    these labels drive older UI headings and value_current/value_prev consumers.
    """
    ordered = sort_periods(periods)
    if not ordered:
        return fallback_current, fallback_prev

    if _is_indian_fiscal_calendar(period_calendar):
        fallback_current_key = _indian_fiscal_period_end_key(fallback_current)
        if fallback_current_key and any(
            _indian_fiscal_period_end_key(period) == fallback_current_key
            for period in ordered
        ):
            current = fallback_current
        else:
            current = ordered[0]

        for candidate in (fallback_prev, previous_period_label(current)):
            candidate_key = _indian_fiscal_period_end_key(candidate)
            if candidate_key and any(
                _indian_fiscal_period_end_key(period) == candidate_key
                for period in ordered
            ):
                return current, candidate
        return current, fallback_prev

    current = ordered[0]
    comparable_prev = previous_period_label(current)
    prev = comparable_prev if comparable_prev in ordered else (ordered[1] if len(ordered) > 1 else fallback_prev)
    return current, prev


_FORWARD_LOOKING_PERIOD_RE = re.compile(
    r"\b(?:forecasts?|forecasted|guidance|outlook|projections?|projected|"
    r"estimates?|estimated|consensus|budgets?|targets?|planned|plans?|"
    r"business\s+plan|medium[-\s]?term\s+plan|"
    r"scenarios?|sensitivity)\b"
    r"|(?:прогноз|прогнозн|планов|плановый|план|ориентир|сценари)"
    r"|(?<![A-Z0-9])(?:FY|CY)?\s*['’]?\d{2,4}\s*[EF](?![A-Z])",
    re.I,
)


def _is_forward_looking_period_header(raw_period: object) -> bool:
    """Return true for period/table headers that explicitly mark forecast data."""
    text = str(raw_period or "").strip()
    if not text:
        return False
    return bool(_FORWARD_LOOKING_PERIOD_RE.search(text))


_RELATIVE_CURRENT_PERIOD_RE = re.compile(
    r"(?:\bcurrent\b|\breporting\b|\bтекущ\w*\b|\bотчетн\w*\b)",
    re.I,
)
_RELATIVE_PREVIOUS_PERIOD_RE = re.compile(
    r"(?:\bprevious\b|\bprior\b|\bcomparative\b|\bpreceding\b|"
    r"\bпредыдущ\w*\b|\bпредшеств\w*\b|\bсравнительн\w*\b)",
    re.I,
)


def _relative_duration_period_from_header(
    raw_period: object,
    *,
    enum: str | None,
    period_current: str,
    period_prev: str,
) -> str:
    """Map generic current/previous report-period headers to run periods."""
    if not enum or period_kind_for_enum(enum) != DURATION_KIND:
        return ""
    text = str(raw_period or "").replace("\u00a0", " ").strip().lower().replace("ё", "е")
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    if _RELATIVE_PREVIOUS_PERIOD_RE.search(text):
        return normalize_period_label(period_prev, value_kind=DURATION_KIND)
    if _RELATIVE_CURRENT_PERIOD_RE.search(text):
        return normalize_period_label(period_current, value_kind=DURATION_KIND)
    return ""


def _coerce_period_values(
    info: dict,
    *,
    enum: str | None = None,
    period_current: str,
    period_prev: str,
    default_unit: str = "",
    page_default: int | None = None,
    abs_cost: bool = False,
    dash_as_zero: bool = False,
    max_report_year: int | None = None,
    fiscal_context: FiscalPeriodContext | None = None,
    period_calendar: str = "calendar",
) -> tuple[list[dict], str]:
    """Parse model JSON into normalized [{period, value, unit, ...}] rows.

    Supports the new full-context shape:
        {"values": {"Year ended January 31, 2024": "1 234"}, "unit_text": "..."}
    and legacy current/previous fields so older mocks / traces still parse.
    """
    if not isinstance(info, dict):
        info = {}

    metric_enum = str(enum or "").strip().upper()
    unit_text_raw = info.get("unit_text", info.get("unit")) or default_unit or ""
    default_currency = _currency_from_unit_text(default_unit)
    metric_unit = _parse_unit_text(unit_text_raw, default_currency) or unit_text_raw or default_unit or ""
    if metric_enum == "EPS":
        metric_unit = _normalise_eps_unit(unit_text_raw, default_unit)

    raw_items: list[tuple[str, object, str, int | None, str]] = []
    values = info.get("values")
    if isinstance(values, dict):
        for p, raw in values.items():
            raw_items.append((str(p), raw, metric_unit, None, str(raw) if raw is not None else ""))

    period_values = info.get("period_values")
    if isinstance(period_values, list):
        for item in period_values:
            if not isinstance(item, dict):
                continue
            p = item.get("period") or item.get("label")
            raw = item.get("value_text", item.get("value"))
            u_raw = item.get("unit_text", item.get("unit")) or unit_text_raw
            u = _parse_unit_text(u_raw, default_currency) or u_raw or metric_unit
            if metric_enum == "EPS":
                u = _normalise_eps_unit(u_raw, default_unit)
            page = item.get("page")
            try:
                page_int = int(page) if page is not None else None
            except Exception:
                page_int = None
            raw_items.append((str(p or ""), raw, u, page_int, str(raw) if raw is not None else ""))

    if not raw_items:
        raw_items = [
            (period_current, info.get("current_text", info.get("current")), metric_unit, None, ""),
            (period_prev, info.get("previous_text", info.get("previous")), metric_unit, None, ""),
        ]

    page_raw = info.get("page", page_default)
    try:
        page_num = int(page_raw) if page_raw is not None else None
    except Exception:
        page_num = None
    if page_num is not None and page_num <= 0:
        page_num = None
    fiscal_context = fiscal_context or FiscalPeriodContext.empty()

    anchor_periods = [period_current, period_prev]
    anchor_periods.extend(raw_period for raw_period, *_ in raw_items)

    by_period: dict[str, dict] = {}
    for raw_period, raw_value, raw_unit, item_page, source_text in raw_items:
        if _is_forward_looking_period_header(raw_period):
            continue
        metric_period_kind = period_kind_for_enum(enum) if enum else None
        relative_period = _relative_duration_period_from_header(
            raw_period,
            enum=enum,
            period_current=period_current,
            period_prev=period_prev,
        )
        period_info = (
            None
            if relative_period
            else (normalize_metric_period(raw_period, enum, fiscal_context=fiscal_context) if enum else None)
        )
        period_info_is_usable = (
            period_info is not None
            and period_info.ok
            and bool(period_info.period)
            and period_info.reason != "period_kind_unrepairable"
        )
        incomplete_duration_period = ""
        if (
            not relative_period
            and enum
            and period_kind_for_enum(enum) == DURATION_KIND
            and not period_info_is_usable
        ):
            incomplete_duration_period = normalize_incomplete_duration_label(
                raw_period,
                tuple(anchor_periods),
            )
        period = (
            relative_period
            or (period_info.period if period_info_is_usable else "")
            or incomplete_duration_period
            or normalize_period_label(raw_period, fiscal_context=fiscal_context)
        )
        indian_raw_period = ""
        if _is_indian_fiscal_calendar(period_calendar) and not relative_period:
            raw_period_clean = str(raw_period or "").replace("\u00a0", " ").strip()
            raw_period_is_anchor = raw_period_clean in {period_current, period_prev}
            indian_raw_period = _normalize_period_calendar_label(
                raw_period_clean,
                period_calendar,
                value_kind=metric_period_kind,
                already_normalized=raw_period_is_anchor,
            )
            if indian_raw_period == raw_period_clean:
                indian_raw_period = ""
        if indian_raw_period:
            period = indian_raw_period
        else:
            period_is_anchor = period in {period_current, period_prev}
            period = _normalize_period_calendar_label(
                period,
                period_calendar,
                value_kind=metric_period_kind,
                already_normalized=period_is_anchor,
            )
        if not period:
            continue
        period_year = _period_year(period)
        if max_report_year is not None and period_year is not None and period_year > max_report_year:
            continue
        if dash_as_zero and (
            raw_value is None
            or str(raw_value).replace("\u00a0", " ").strip() in {"", "-", "—", "–", "−"}
        ):
            v = 0.0
        else:
            v = _parse_verbatim_value(raw_value)
        if metric_enum == "MODIFIED_AUDITORS_OPINION":
            unit = "boolean"
        elif metric_enum == "EPS":
            unit = _normalise_eps_unit(raw_unit or metric_unit, default_unit)
        else:
            v, norm_unit = _normalise_money_value(v, raw_unit or metric_unit)
            unit = norm_unit or raw_unit or metric_unit
        by_period[period] = {
            "period": period,
            "raw_period": str(raw_period or ""),
            "period_normalization_reason": (
                "relative_report_period_header"
                if relative_period
                else (
                    "incomplete_duration_header"
                    if incomplete_duration_period
                    else (period_info.reason if period_info else "")
                )
            ),
            "value": v,
            "unit": unit,
            "page": item_page or page_num,
            "source_text": source_text or (str(raw_value) if raw_value is not None else ""),
        }

    return (
        [by_period[p] for p in sort_periods(list(by_period.keys()))],
        metric_unit,
    )


def _value_for_period(
    period_values: list[dict],
    target: str,
    period_calendar: str = "calendar",
) -> Optional[float]:
    target_norm = normalize_period_label(target)
    for item in period_values:
        if normalize_period_label(item.get("period")) == target_norm:
            return item.get("value")
    if _is_indian_fiscal_calendar(period_calendar):
        target_key = _indian_fiscal_period_end_key(target_norm)
        if target_key is not None:
            for item in period_values:
                if _indian_fiscal_period_end_key(item.get("period")) == target_key:
                    return item.get("value")
    return None


def _first_value(period_values: list[dict]) -> Optional[float]:
    for item in period_values:
        if item.get("value") is not None:
            return item.get("value")
    return None


def _page_from_period_values(period_values: list[dict]) -> int | None:
    for item in period_values:
        page = item.get("page")
        if isinstance(page, int) and page > 0:
            return page
    return None


def _main_total_fallback_event(
    *,
    metric: dict,
    period_values: list[dict],
    period_current: str,
    period_prev: str,
    unit: str,
) -> dict:
    v_cur = _value_for_period(period_values, period_current)
    if v_cur is None:
        v_cur = _first_value(period_values)
    return {
        "type": "metric",
        "enum": metric["enum"],
        "name_ru": metric.get("name_ru") or metric.get("name_en") or metric["enum"],
        "value_current": v_cur,
        "value_prev": _value_for_period(period_values, period_prev),
        "period_values": period_values,
        "unit": (period_values[0].get("unit") if period_values else None) or unit,
        "found": any(p.get("value") is not None for p in period_values),
        "is_calculated": False,
        "formula": "",
        "page": _page_from_period_values(period_values),
        "segment": "TOTAL",
        "dimension": "main_total_fallback",
        "segment_fallback": "main_metric_total",
    }


def _metric_block_iter(buffer: str, valid_enums: set[str]):
    """Yield (enum, json_object_text) for complete metric blocks in a JSON stream.

    Unlike the old regex, this handles nested objects such as
    {"values": {"Year ended January 31, 2024": "..."}} and arrays. It is intentionally tolerant:
    it scans for `"ENUM": {` and then waits until braces balance outside
    strings.
    """
    import re as _re
    for m in _re.finditer(r'"([A-Z][A-Z0-9_]+)"\s*:\s*\{', buffer):
        enum = m.group(1)
        if enum not in valid_enums:
            continue
        start = buffer.find("{", m.end() - 1)
        if start < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(buffer)):
            ch = buffer[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield enum, buffer[start:i + 1]
                    break


_SEGMENT_METRICS_IFRS_RU: set[str] = {
    "SALES_REVENUE",
    "OPERATING_PROFIT",
    "NET_INCOME",
    "INTEREST_PAID",
    "EBITDA",
}
_SEGMENT_METRICS_US_GAAP_EN: set[str] = {
    "SALES_REVENUE",
    "GROSS_PROFIT",
    "OPERATING_PROFIT",
    "NET_INCOME",
    "COSTS_EXPENSES",
}


def _financial_analyst_intro_ru(reporting_standard: str) -> str:
    return (
        "Ты опытный финансовый аналитик. Извлекаешь значения метрик из финансового "
        "отчёта стандарта {reporting_standard}.\n"
        "Ты аккуратно сопоставляешь строки отчёта с описаниями, синонимами и подсказками "
        "по каждой метрике. Если строка похожа на нужный показатель, но не совпадает с "
        "его смыслом или включает лишние компоненты, возвращай null: ложное значение "
        "хуже пропуска."
    ).format(reporting_standard=reporting_standard)


def _financial_analyst_intro_en(
    standard: str,
    document_type: str = "financial_report",
) -> str:
    if document_type != "financial_report":
        source_label = _NON_REPORT_SOURCE_LABELS_EN.get(
            document_type,
            "source document",
        )
        return (
            "You are a senior financial analyst extracting business breakdowns "
            "from the supplied {source_label}, which reports results under "
            "{standard}. Match each disclosed value against the target metric "
            "description, aliases and hint. If a value is similar but not the "
            "requested metric, or includes extra components, return nothing for "
            "it: a false value is worse than a miss. Accept values explicitly "
            "stated in narrative text, tables, KPI highlights or labelled charts "
            "when the source directly ties the value to a named business, product "
            "or geography breakdown and period. Do not invent allocations."
        ).format(source_label=source_label, standard=standard)
    return (
        "You are a senior financial analyst extracting business breakdowns from "
        "{standard} financial reports. Match each row against the target metric "
        "description, aliases and hint. If a row is similar but not the requested "
        "metric, or includes extra components, return nothing for it: a false value "
        "is worse than a miss. Return only values explicitly present in segment, "
        "geography or product tables. Do not invent allocations."
    ).format(standard=standard)


def _period_rules_ru() -> str:
    return (
        "## Правила периодов\n"
        "Возвращай ключ периода в `values` буквально как в шапке таблицы: "
        "`Year ended January 31, 2013`, `За год, закончившийся 31 декабря 2024`, "
        "`3 месяца, закончившиеся 31 марта 2026`. Не сокращай такие заголовки до "
        "`FY`, `Q1`, `6M`, `9M`, `12M` и не заменяй их одной датой. "
        "Python-постобработчик сам нормализует период с учётом типа метрики. "
        "Если в шапке один объединённый заголовок для нескольких колонок, можно "
        "повторить полную формулировку для каждой колонки, сохранив видимую дату "
        "окончания периода. Если шапка действительно уже написана как `Q1 2025`, "
        "`6M 2025` или `31.12.2025`, верни её как написано."
    )


def _period_rules_en(
    document_type: str = "financial_report",
) -> str:
    if document_type != "financial_report":
        return (
            "Return period keys in `values` literally as they appear in the nearest "
            "heading, table header, bullet or sentence, for example `Fourth quarter "
            "ended February 1, 2025` or `Three months ended March 31, 2026`. Do not "
            "shorten or reinterpret them as `FY`, `Q1`, `6M`, `9M` or `12M` unless "
            "the source itself uses that label. The Python post-processor will "
            "normalize the period using the metric type. Every extracted prose value "
            "must have an explicit nearby completed-period label; do not guess a "
            "period from the document publication date."
        )
    return (
        "Return period keys in `values` literally as they appear in the table "
        "header: `Year ended January 31, 2013`, `Three months ended March 31, "
        "2026`, `As of December 31, 2025`. Do not shorten these headers to "
        "`FY`, `Q1`, `6M`, `9M`, `12M` and do not replace them with a bare date. "
        "The Python post-processor will normalize the period using the metric "
        "type. If one merged header spans several columns, you may repeat the "
        "full wording for each column while preserving the visible period-end "
        "date. If the source header itself is already `Q1 2025`, `6M 2025` or "
        "`31.12.2025`, return it as written."
    )


def _verbatim_value_rules_ru() -> str:
    return (
        "## КРИТИЧЕСКОЕ правило: возвращай значение БУКВАЛЬНО как написано в источнике\n"
        "В объекте `values` каждое значение должно быть строкой **точно как в ячейке "
        "таблицы PDF** — со всеми пробелами, запятыми, скобками. НЕ конвертируй "
        "единицы, НЕ нормализуй разделители, НЕ удаляй скобки. Деривация числа — "
        "задача детерминированного кода после тебя, не твоя.\n\n"
        "Поле `unit_text` — буквально из заголовка таблицы / шапки колонки / шапки "
        "документа («in millions», «$ in millions», «в млрд руб.», «тыс. руб.», "
        "«руб.», «%»). Бери ближайший видимый источник единицы; для разных метрик "
        "она может различаться. Не подставляй рубли для US GAAP / USD-отчётности.\n\n"
        "### ПРИМЕРЫ\n\n"
        "ИСТОЧНИК: «Чистый процентный доход | 2 999,6 | 2 564,6» под заголовком "
        "«(в миллиардах российских рублей)»\n"
        "WRONG: {\"values\": {\"FY 2024\": \"2999600000\"}, \"unit_text\": \"млн руб\"}\n"
        "WRONG: {\"values\": {\"FY 2024\": \"2999.6\"}, \"unit_text\": \"млн руб\"}\n"
        "RIGHT: {\"values\": {\"За год, закончившийся 31 декабря 2024 года\": "
        "\"2 999,6\", \"За год, закончившийся 31 декабря 2023 года\": \"2 564,6\"}, "
        "\"unit_text\": \"в миллиардах российских рублей\"}\n\n"
        "ИСТОЧНИК: «Расходы по налогу | (454,3) | (250,0)» в той же таблице\n"
        "WRONG: {\"values\": {\"FY 2024\": \"-454.3\"}} или "
        "{\"values\": {\"FY 2024\": \"454.3\"}}\n"
        "RIGHT: {\"values\": {\"За год, закончившийся 31 декабря 2024 года\": "
        "\"(454,3)\", \"За год, закончившийся 31 декабря 2023 года\": \"(250,0)\"}}\n"
        "Если в ячейке скобки, они обязательны: `(465)` -> `(465)`, не `465`.\n\n"
        "ИСТОЧНИК: «Дивиденды на акцию | 35 | 30» под заголовком «(в рублях)»\n"
        "RIGHT: {\"values\": {\"2024\": \"35\"}, \"unit_text\": \"в рублях\"}\n\n"
        "Если строка нужной метрики найдена, но ячейка периода содержит только "
        "прочерк (`-`, `—`, `–`, `−`) или пустое значение, верни строку \"0\" "
        "для этого периода: это означает отсутствие движения/суммы в найденной "
        "строке, а не отсутствие метрики. Если самой строки/показателя в отчёте "
        "нет — используй null или пустой `values`."
    )


def _verbatim_value_rules_en(
    document_type: str = "financial_report",
) -> str:
    if document_type != "financial_report":
        return (
            "Return each numeric value exactly as it appears in the source sentence, "
            "bullet, table cell or chart label. Do NOT convert units, normalize "
            "thousands separators, or remove commas, spaces or parentheses. Put the "
            "nearest explicitly stated currency and scale into `unit_text`. For "
            'example, source text "$12.72 billion" becomes value "12.72" with '
            '`unit_text: "$ billion"`; never convert it to "12,720". If an explicit '
            "table row is present but its period cell is blank or contains only -, —, "
            "– or −, return \"0\" for that period. Use null only when the requested "
            "metric value is not disclosed."
        )
    return (
        "Return each value exactly as it appears in the table cell. Do NOT convert "
        "units, do NOT normalize thousands separators, do NOT remove commas, spaces "
        'or parentheses. If the table says "25,785", return "25,785", not "25785" '
        'and not "25.785". If the table says "(4,190)", return "(4,190)"; '
        'parentheses are mandatory: "(465)" must stay "(465)", not "465". '
        "If the requested metric row is present but a period cell is blank or "
        "contains only -, —, – or −, return \"0\" for that period. Use null only "
        "when the metric row itself is absent or the value is not disclosed. "
        "Put the table/header unit literally into unit_text, for example "
        '"in millions" or "$ in millions".'
    )


def _actual_values_only_rules_ru() -> str:
    return (
        "## Только фактические значения\n"
        "Извлекай только фактические исторические/отчётные значения из финансовой "
        "отчётности и примечаний за уже завершившиеся периоды. Не извлекай прогнозы, "
        "планы, бюджеты, ориентиры, цели, сценарии, sensitivity analysis, guidance, "
        "outlook, forecast, projection, target и любые forward-looking значения, даже "
        "если строка и метрика похожи на нужные. Если нужная метрика встречается только "
        "в прогнозной таблице или forecast/outlook/guidance-разделе — верни null / "
        "пустой values.\n"
        "Не считай прогнозом обычные фактические бухгалтерские оценки внутри отчётности "
        "(например ECL/ожидаемые кредитные убытки, оценочные обязательства, отложенные "
        "налоги или пенсионные расчёты), если они отражены как фактические значения "
        "за отчётный период."
    )


def _actual_values_only_rules_en(
    document_type: str = "financial_report",
) -> str:
    if document_type != "financial_report":
        source_label = _NON_REPORT_SOURCE_LABELS_EN.get(
            document_type,
            "source document",
        )
        return (
            "## Actual values only\n"
            "Extract only actual historical/reported values explicitly stated in "
            f"the {source_label}'s narrative text, tables, KPI highlights or "
            "labelled charts for periods that have already ended. Do NOT extract "
            "forecasts, plans, budgets, targets, scenarios, sensitivity analysis, "
            "guidance, outlook, projections, projected/forecasted values or any "
            "other forward-looking amounts, even when the wording resembles a "
            "target metric. If a requested metric appears only in a forecast, "
            "outlook or guidance section, return no segment row for it."
        )
    return (
        "## Actual values only\n"
        "Extract only actual historical/reported values from financial statements "
        "and notes for periods that have already ended. Do NOT extract forecasts, "
        "plans, budgets, targets, scenarios, sensitivity analysis, guidance, outlook, "
        "projections, projected/forecasted values or any other forward-looking "
        "amounts, even when the row label looks like a target metric. If a requested "
        "metric appears only in a forecast/outlook/guidance table or section, return "
        "null / an empty values object for it.\n"
        "Do not treat ordinary accounting estimates inside actual financial "
        "statements as forecasts: expected credit losses, provisions, deferred taxes "
        "and pension assumptions may be valid actual-period disclosures when they are "
        "reported in the statements or notes."
    )


_NON_REPORT_SOURCE_LABELS_RU = {
    "press_release": "пресс-релиз",
    "transcript": "транскрипт звонка с менеджментом",
    "presentation": "IR-презентация",
    "equity_research": "equity research стороннего аналитика",
}

_NON_REPORT_SOURCE_LABELS_EN = {
    "press_release": "press release",
    "transcript": "management call transcript",
    "presentation": "IR presentation",
    "equity_research": "third-party equity research report",
}


def _source_specific_actual_rules_ru(document_type: str) -> str:
    label = _NON_REPORT_SOURCE_LABELS_RU.get(document_type)
    if not label:
        return ""
    extra = {
        "press_release": (
            "Разделы outlook/guidance и таблицы с целями полностью игнорируй."
        ),
        "transcript": (
            "В Q&A не принимай гипотезы, ожидания и ответы о будущих периодах за факты."
        ),
        "presentation": (
            "Не используй стратегические цели, целевую модель и слайды outlook."
        ),
        "equity_research": (
            "В таблицах аналитика используй только колонки Actual/Reported/Historical "
            "или с суффиксом A; колонки E/F, consensus, estimates и price targets игнорируй."
        ),
    }[document_type]
    return (
        f"## Правила для типа источника: {label}\n"
        "Это не полная финансовая отчетность. Явно раскрытые в тексте или таблицах "
        "исторические факты за завершенный период допустимы, даже если основных "
        "финансовых форм в документе нет.\n"
        "Извлекай только непосредственно приведенное фактическое значение. Не выводи "
        "его из темпов роста, маржи, компонент или слов менеджмента косвенно. Не "
        "подменяй базовый MetricENUM вариантом adjusted, normalized, organic, pro forma "
        "или underlying; если раскрыта только такая версия, верни null.\n"
        "Предварительно найденные Python периоды являются только навигационными "
        "подсказками и не доказывают, что значение фактическое. "
        + extra
    )


def _source_specific_actual_rules_en(document_type: str) -> str:
    label = _NON_REPORT_SOURCE_LABELS_EN.get(document_type)
    if not label:
        return ""
    extra = {
        "press_release": "Ignore outlook/guidance sections and target tables entirely.",
        "transcript": (
            "In Q&A, do not treat hypotheticals, expectations or answers about future "
            "periods as facts."
        ),
        "presentation": (
            "Do not use strategic targets, target operating models or outlook slides."
        ),
        "equity_research": (
            "In analyst tables, use only Actual/Reported/Historical columns or an A "
            "suffix; ignore E/F columns, consensus, estimates and price targets."
        ),
    }[document_type]
    return (
        f"## Rules for source type: {label}\n"
        "This is not a full financial report. Historical facts explicitly disclosed "
        "in its text or tables for a completed period are valid even when primary "
        "financial statements are absent.\n"
        "Extract only a directly stated actual value. Do not infer it from growth "
        "rates, margins, components or indirect management wording. Do not substitute "
        "a base MetricENUM with an adjusted, normalized, organic, pro forma or "
        "underlying variant; if only that variant is disclosed, return null.\n"
        "Python pre-detected periods are navigation hints only and are not evidence "
        "that a value is actual. "
        + extra
    )


def _role_wants_segments(role: str | None) -> bool:
    r = (role or "").lower()
    return "диэ" in r or "die" in r


def _detect_reporting_standard(text: str) -> str:
    head = (text or "")[:120_000]
    low = head.lower()
    if "u.s. gaap" in low or "us gaap" in low or "united states securities" in low:
        return "us_gaap_en"
    if (
        "ind as" in low
        or "ind-as" in low
        or "indian accounting standard" in low
        or "companies (indian accounting standards)" in low
    ):
        return "ind_as_en"
    if (
        "generally accepted accounting principles in india" in low
        or "banking regulation act, 1949" in low
        or "reserve bank of india" in low
        or "rbi guidelines" in low
    ):
        return "ind_gaap_en"
    # A cheap language fallback: enough for the current RU IFRS / EN US GAAP split.
    cyr = sum(1 for ch in head[:20_000] if "а" <= ch.lower() <= "я")
    lat = sum(1 for ch in head[:20_000] if "a" <= ch.lower() <= "z")
    return "ifrs_ru" if cyr >= lat * 0.35 else "us_gaap_en"


def _segment_metric_scope(metrics: list[dict], standard: str) -> list[dict]:
    wanted = _SEGMENT_METRICS_US_GAAP_EN if standard == "us_gaap_en" else _SEGMENT_METRICS_IFRS_RU
    out: list[dict] = []
    for m in metrics:
        enum = m.get("enum")
        if enum in wanted:
            out.append(m)
    # US GAAP benchmark calls it OPERATING_INCOME. The registry uses the
    # broader OPERATING_PROFIT enum, so the segment extractor is instructed
    # to treat "operating income" as the same metric and the benchmark maps it.
    return out


def _normalize_segment_mode(segment_mode: str | None) -> str:
    value = (segment_mode or "extended").strip().lower().replace("-", "_")
    return "core" if value in {"core", "required", "main", "basic"} else "extended"


def _segment_rules_en(
    segment_mode: str | None,
    document_type: str = "financial_report",
) -> str:
    mode = _normalize_segment_mode(segment_mode)
    if document_type != "financial_report":
        source_guidance = {
            "press_release": (
                "In a press release, search financial highlights, business-unit or "
                "segment results, KPI tables, reconciliations and narrative sentences "
                "that directly pair a named breakdown with the target metric and period."
            ),
            "presentation": (
                "In an IR presentation, search KPI and results slides, tables and "
                "labelled charts. Use slide titles, legends and footnotes to identify "
                "the metric, period and unit, but extract only explicitly printed values."
            ),
            "transcript": (
                "In a transcript, use only explicit actual values stated by management "
                "for a named breakdown and completed period; do not infer values from "
                "growth commentary or analyst questions."
            ),
            "equity_research": (
                "In equity research, keep the covered issuer's disclosed business "
                "breakdowns and only Actual/Reported/Historical values; ignore estimates "
                "and the research publisher's own organizational structure."
            ),
        }.get(
            document_type,
            "Use only explicit actual values tied to a named business breakdown and period.",
        )
        core = (
            "A business breakdown is only one of these dimensions:\n"
            "- operating or reportable segments named by the company;\n"
            "- divisions, business units, products/product groups or business lines;\n"
            "- geography, regions or markets.\n\n"
            "These are REQUIRED segment rows. A formal IFRS 8 / ASC 280 segment note "
            "or the literal word `segment` is not required in a non-report source. "
            "Use the company's own breakdown names and disclosure level. Ignore payment "
            "methods, customer settlement channels, legal entities, sales channels and "
            "other administrative splits unless the source clearly presents them as "
            "operating businesses. Map divisions, business units and business lines to "
            '`dimension: "operating_segment"`, products/product groups to '
            '`dimension: "product"`, and regions/markets to `dimension: "geography"`. '
            "A prose sentence is eligible only when it directly states the named "
            "breakdown, target metric, actual period and value. Do not derive a segment "
            "value from growth rates, shares, margins or a consolidated total. If a "
            "disclosure is nested inside a parent business, keep the parent in the label "
            "in extended mode (for example `Parent business|Region`); in core mode, skip "
            "the nested cross-breakdown instead of promoting the child label to a "
            "company-level segment.\n\n"
            + source_guidance
        )
        if mode == "core":
            return (
                core
                + "\n\nExtract only REQUIRED segment rows. Do not extract cross-breakdowns "
                "that intersect several segment dimensions."
            )
        return (
            core
            + "\n\nAlso extract explicit cross-breakdowns that intersect several "
            "required dimensions, for example geography by product or product by "
            "operating segment. Join intersected labels with `|`, for example "
            "`Russia|Retail` or `Europe|Lubricants`."
        )

    core = (
        "A segment is only one of these business breakdowns:\n"
        "- operating segments reported by the company;\n"
        "- products/product groups/business lines, which may be different from operating segments;\n"
        "- geography/regions/markets.\n\n"
        "These are REQUIRED segment rows. Ignore payment methods, customer settlement "
        "channels, legal entities, sales channels and other administrative splits unless "
        "the report clearly presents them as operating segments. Prefer the decomposition "
        "the company itself treats as primary in the segment section / segment note "
        "(IFRS operating segments, US GAAP segment note), but do not stop at that table. "
        "If the report separately discloses the requested metric by product, product group, "
        "business line, geography or region, extract those rows too. Products often overlap "
        "with operating segments, but when they are disclosed as a separate business "
        "breakdown they are still REQUIRED segment rows. Look across all tables that break "
        "down the requested metrics, not only the most obvious operating-segment table. "
        "Use geography or products only when they are clearly presented as business "
        "breakdowns, not incidental customer/payment splits. Preserve the disclosure level: "
        "do not promote a geography/product breakdown that belongs inside one specific "
        "segment or product to a company-level segment row. If a table is nested inside "
        "a parent segment/product, keep the parent in the segment label in extended mode "
        "(for example `Parent segment|Region`); in core mode, skip such nested cross-"
        "breakdowns instead of returning the child label alone."
    )
    if mode == "core":
        return (
            core
            + "\n\nExtract only REQUIRED segment rows. Do not extract cross-breakdowns "
            "that intersect several segment dimensions."
        )
    return (
        core
        + "\n\nAlso extract additional cross-breakdowns when clearly disclosed. "
        "In extended mode, these additional cross-breakdown rows are part of the "
        "requested output, not a lower-priority task. A cross-breakdown is an "
        "intersection of several required segment dimensions, for example geography "
        "by product or product by operating segment. Treat matrix/pivot-style cells "
        "and nested rows where one required dimension is broken down by another as "
        "additional cross-breakdowns. A flat table that presents one required "
        "dimension at a time, such as only products or only geography, is not a "
        "cross-breakdown and should be handled as REQUIRED rows. For cross-breakdown rows, set "
        "`segment` to the intersected labels joined with `|`, for example "
        "`Russia|Retail` or `Europe|Lubricants`."
    )


def _build_segment_context(text: str, *, max_chars: int = 120_000) -> str:
    """Select segment/business breakdown pages from full report text."""
    src = text or ""
    keywords = (
        "сегмент", "segment", "географ", "geograph", "region", "market",
        "выруч", "revenue", "sales", "ebitda", "operating profit",
        "operating income", "gross profit", "cost of", "costs", "expenses",
        "interest paid", "процент", "продукт", "product",
    )
    page_re = re.compile(r"===PAGE\s+(\d+)===\n.*?(?=\n\n===PAGE\s+\d+===|\Z)", re.S)
    page_matches = list(page_re.finditer(src))
    pages = [m.group(0) for m in page_matches]
    if pages:
        segment_labels = (
            "data center", "client", "gaming", "embedded", "all other",
            "commercial airplanes", "defense, space & security", "global services",
            "united states", "china", "singapore", "taiwan", "japan", "europe",
            "upstream", "downstream", "upstream u.s.", "upstream int'l",
            "downstream u.s.", "downstream int'l", "segment total",
        )

        def _numeric_table_score(page: str) -> int:
            score = 0
            for line in page.splitlines():
                low_line = line.lower()
                has_number = bool(re.search(r"\d", line))
                if has_number and ("|" in line or re.search(r"\s{2,}", line)):
                    score += 1
                if has_number and any(label in low_line for label in segment_labels):
                    score += 2
            return min(score, 30)

        def _segment_page_score(page: str) -> int:
            low = page.lower()
            score = 0
            if "note 4" in low and ("segment" in low or "сегмент" in low):
                score += 80
            if "segment reporting" in low or "reportable segments" in low:
                score += 50
            if "summary of net revenue and operating income" in low:
                score += 70
            if "sales to external customers by geographic" in low:
                score += 70
            if "net revenue:" in low or "total net revenue" in low:
                score += 35
            if "operating income (loss)" in low or "total operating income" in low:
                score += 35
            if "gross profit" in low:
                score += 20
            if "sales and other operating revenues" in low:
                score += 45
            if "total costs and other deductions" in low:
                score += 45
            if "net income (loss) attributable" in low:
                score += 35
            if "segment total" in low and "all other" in low:
                score += 50
            if "upstream u.s." in low and "downstream u.s." in low:
                score += 45
            if "upstream int'l" in low and "downstream int'l" in low:
                score += 45
            if "segment assets" in low and not any(
                term in low
                for term in (
                    "sales and other operating revenues",
                    "total costs and other deductions",
                    "net income",
                    "operating income",
                )
            ):
                score -= 35
            distinct_period_headers = len(
                set(
                    re.findall(
                        r"(?:years? ended|three months ended|six months ended|nine months ended)"
                        r"\s+[a-z]+\s+\d{1,2},\s+\d{4}",
                        low,
                    )
                )
            )
            labels_hit = sum(1 for label in segment_labels if label in low)
            if labels_hit >= 2:
                score += min(labels_hit * 8, 48)
                # Prefer the complete segment note over abbreviated MD&A
                # duplicates that often show only the latest year.
                score += min(distinct_period_headers * 100, 300)
            score += _numeric_table_score(page)
            score += sum(3 for k in keywords if k in low)
            return score

        scored = [
            (i, int(page_matches[i].group(1)), _segment_page_score(page), page)
            for i, page in enumerate(pages)
        ]
        by_idx = {idx: (page_no, score, page) for idx, page_no, score, page in scored}
        selected_idxs: set[int] = set()
        for idx, _page_no, score, _page in scored:
            if score >= 45:
                selected_idxs.add(idx)
                if idx - 1 in by_idx:
                    selected_idxs.add(idx - 1)
                if idx + 1 in by_idx:
                    selected_idxs.add(idx + 1)
        if not selected_idxs:
            selected_idxs = {idx for idx, _page_no, score, _page in scored if score > 0}
        selected_scored = [
            (idx, page_no, score, page)
            for idx, (page_no, score, page) in by_idx.items()
            if idx in selected_idxs
        ]
        selected_scored.sort(key=lambda item: (-item[2], item[1], item[0]))
        selected = [page for _idx, _page_no, _score, page in selected_scored]
        if not selected:
            selected = pages
        out = "\n\n".join(selected)
        return out[:max_chars]

    lows = src.lower()
    spans: list[tuple[int, int]] = []
    for k in keywords:
        start = 0
        while True:
            i = lows.find(k, start)
            if i < 0:
                break
            spans.append((max(0, i - 2500), min(len(src), i + 7000)))
            start = i + len(k)
            if len(spans) > 40:
                break
        if len(spans) > 40:
            break
    if not spans:
        return src[:max_chars]
    spans.sort()
    merged: list[tuple[int, int]] = []
    for a, b in spans:
        if not merged or a > merged[-1][1] + 500:
            merged.append((a, b))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
    parts = []
    total = 0
    for a, b in merged:
        chunk = src[a:b]
        if total + len(chunk) > max_chars:
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n\n---\n\n".join(parts)


def _normalize_segment_context_mode(mode: str | None) -> str:
    value = (mode or "full").strip().lower().replace("-", "_")
    if value in {"focused", "focus", "anchors", "anchor", "selected", "pages"}:
        return "focused"
    return "full"


def _build_segment_prompt_context(text: str, *, mode: str | None = "full") -> str:
    """Return the text sent to the segment LLM stage.

    Full text is the default because the segment tables are often far from
    their textual anchors and OCR JSON can contain duplicate page markers for
    separate tables. The old anchor/page selector remains available as
    ``focused`` for token-budget experiments.
    """
    src = text or ""
    if _normalize_segment_context_mode(mode) == "focused":
        return _build_segment_context(src)
    return src


_MONTH_PATTERN_EN = (
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},\s*\n?\s*\d{4}"
)


def _clean_segment_line(line: str) -> str:
    return (
        (line or "")
        .replace("\u00a0", " ")
        .replace("\u2009", " ")
        .replace("\ufeff", "")
        .strip()
    )


def _segment_periods_from_block(block: str) -> list[str]:
    periods: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(_MONTH_PATTERN_EN, block, flags=re.I):
        label = "Year Ended " + " ".join(match.group(0).replace("\n", " ").split())
        if label not in seen:
            seen.add(label)
            periods.append(label)
    return periods[:6]


def _looks_like_segment_number(line: str) -> bool:
    s = _clean_segment_line(line)
    if s in {"", "$"}:
        return False
    if s in {"-", "—", "–", "−", "*"}:
        return True
    return bool(re.fullmatch(r"\(?-?\d[\d,\s]*(?:\.\d+)?\)?%?", s))


def _looks_like_segment_label(line: str) -> bool:
    s = _clean_segment_line(line)
    if not s or s == "$" or _looks_like_segment_number(s):
        return False
    low = s.lower()
    if low in {"table of contents", "year ended", "years ended", "(in millions)", "segment"}:
        return False
    if re.fullmatch(r"\d+", s):
        return False
    return any(ch.isalpha() for ch in s)


def _segment_page_no(block: str) -> int | None:
    m = re.search(r"===PAGE\s+(\d+)===", block)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _slice_between(text: str, start_re: str, stop_res: tuple[str, ...]) -> str:
    start = re.search(start_re, text, flags=re.I | re.S)
    if not start:
        return ""
    end_pos = len(text)
    for stop_re in stop_res:
        stop = re.search(stop_re, text[start.end():], flags=re.I | re.S)
        if stop:
            end_pos = min(end_pos, start.end() + stop.start())
    return text[start.start():end_pos]


def _parse_linewise_segment_rows(
    block: str,
    *,
    enum: str,
    dimension: str,
    section_start: str | None = None,
    section_stops: tuple[str, ...] = (),
    label_allow: set[str] | None = None,
    unit_text: str = "in millions",
) -> list[dict]:
    periods = _segment_periods_from_block(block)
    if not periods:
        return []
    section = block
    if section_start:
        section = _slice_between(block, re.escape(section_start), tuple(re.escape(s) for s in section_stops))
    if not section.strip():
        return []

    lines = [_clean_segment_line(line) for line in section.splitlines()]
    rows: list[dict] = []
    page = _segment_page_no(block)
    i = 0
    while i < len(lines):
        label = lines[i]
        if label_allow is not None and label not in label_allow:
            i += 1
            continue
        if label_allow is None and not _looks_like_segment_label(label):
            i += 1
            continue

        values: list[str] = []
        j = i + 1
        while j < len(lines) and len(values) < len(periods):
            item = lines[j]
            if item == "$":
                j += 1
                continue
            if _looks_like_segment_number(item):
                values.append(item)
                j += 1
                continue
            if item:
                break
            j += 1

        if len(values) >= min(2, len(periods)):
            values_by_period = {
                period: value
                for period, value in zip(periods, values)
            }
            rows.append(
                {
                    "enum": enum,
                    "segment": "TOTAL" if _is_total_segment_label(label) else label,
                    "dimension": dimension,
                    "values": values_by_period,
                    "unit_text": unit_text,
                    "page": page,
                }
            )
            i = max(j, i + 1)
        else:
            i += 1
    return rows


def _split_markdown_segment_cells(line: str) -> list[str]:
    if "|" not in (line or ""):
        return []
    stripped = _clean_segment_line(line)
    if not stripped.startswith("|"):
        return []
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = stripped[1:].split("|")
    return [
        re.sub(r"\s+", " ", re.sub(r"<br\s*/?>", " ", cell, flags=re.I)).strip()
        for cell in cells
    ]


def _is_markdown_separator_row(cells: list[str]) -> bool:
    if not cells:
        return False
    return all(
        not cell or re.fullmatch(r":?-{3,}:?", cell.replace(" ", ""))
        for cell in cells
    )


def _is_ifrs_product_revenue_header(label: str) -> bool:
    low = (label or "").lower()
    if "обязан" in low:
        return False
    return (
        "по видам продук" in low
        or "по виду продук" in low
        or "by product" in low
        or "types of products" in low
    )


def _parse_ifrs_product_revenue_segment_rows(text: str) -> list[dict]:
    rows: list[dict] = []
    lines = (text or "").splitlines()
    current_page: int | None = None
    last_revenue_heading_line = -10_000
    i = 0
    while i < len(lines):
        line = lines[i]
        page_match = re.search(r"===PAGE\s+(\d+)===", line)
        if page_match:
            try:
                current_page = int(page_match.group(1))
            except Exception:
                current_page = None

        low_line = line.lower()
        if "выручка" in low_line or "revenue" in low_line:
            last_revenue_heading_line = i

        cells = _split_markdown_segment_cells(line)
        if not cells or _is_markdown_separator_row(cells):
            i += 1
            continue
        header = cells[0]
        near_revenue_heading = i - last_revenue_heading_line <= 80
        if not _is_ifrs_product_revenue_header(header) or not near_revenue_heading:
            i += 1
            continue

        periods = [cell for cell in cells[1:] if cell]
        if len(periods) < 2:
            i += 1
            continue

        j = i + 1
        while j < len(lines):
            row_cells = _split_markdown_segment_cells(lines[j])
            if not row_cells:
                break
            if _is_markdown_separator_row(row_cells):
                j += 1
                continue
            label = row_cells[0].strip()
            if not label:
                j += 1
                continue
            values = row_cells[1: 1 + len(periods)]
            if len(values) < len(periods):
                values = [*values, *([""] * (len(periods) - len(values)))]
            numeric_values = sum(1 for value in values if _looks_like_segment_number(value))
            if numeric_values < min(2, len(periods)):
                j += 1
                continue
            rows.append(
                {
                    "enum": "SALES_REVENUE",
                    "segment": "TOTAL" if _is_total_segment_label(label) else label,
                    "dimension": "product",
                    "values": {
                        period: value
                        for period, value in zip(periods, values)
                        if value
                    },
                    "unit_text": "",
                    "page": current_page,
                }
            )
            j += 1
        i = max(j, i + 1)
    return rows


def _deterministic_segment_rows_from_text(text: str, standard: str) -> list[dict]:
    """Extract obvious line-wise segment tables when the LLM returns no rows."""
    src = text or ""
    rows: list[dict] = []
    if standard != "us_gaap_en":
        rows.extend(_parse_ifrs_product_revenue_segment_rows(src))
    else:
        segment_labels = {
            "Data Center",
            "Client",
            "Gaming",
            "Embedded",
            "All Other",
            "Total net revenue",
            "Total operating income",
        }
        segment_blocks = re.finditer(
            r"The following table provides a summary of net revenue and operating income[\s\S]{0,9000}",
            src,
            flags=re.I,
        )
        for match in segment_blocks:
            block = match.group(0)
            rows.extend(
                _parse_linewise_segment_rows(
                    block,
                    enum="SALES_REVENUE",
                    dimension="operating_segment",
                    section_start="Net revenue:",
                    section_stops=("Cost of sales and operating expenses:", "Operating income (loss):"),
                    label_allow=segment_labels,
                )
            )
            rows.extend(
                _parse_linewise_segment_rows(
                    block,
                    enum="OPERATING_PROFIT",
                    dimension="operating_segment",
                    section_start="Operating income (loss):",
                    section_stops=(
                        "Segment cost of sales",
                        "The following table provides items",
                        "The Company does not",
                    ),
                    label_allow=segment_labels,
                )
            )

        geography_labels = {
            "United States",
            "China (including Hong Kong)",
            "Singapore",
            "Taiwan",
            "Japan",
            "Europe",
            "Other regions",
            "Total sales to external customers",
        }
        geography_blocks = re.finditer(
            r"The following table summarizes sales to external customers by geographic regions[\s\S]{0,5000}",
            src,
            flags=re.I,
        )
        for match in geography_blocks:
            block = match.group(0)
            rows.extend(
                _parse_linewise_segment_rows(
                    block,
                    enum="SALES_REVENUE",
                    dimension="geography",
                    section_start=None,
                    section_stops=(),
                    label_allow=geography_labels,
                )
            )

    best_by_key: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        key = (
            str(row.get("enum") or ""),
            str(row.get("segment") or "").lower(),
            str(row.get("dimension") or "").lower(),
        )
        existing = best_by_key.get(key)
        if existing is None or len(row.get("values") or {}) > len(existing.get("values") or {}):
            best_by_key[key] = row
            continue
    return list(best_by_key.values())


async def _extract_segment_breakdown_full_context(
    *,
    pdf_text_for_prompt: str,
    extractable: list[dict],
    period_current: str,
    period_prev: str,
    company_display: str,
    effective_type: str,
    model_id: Optional[str],
    segment_mode: str = "extended",
    segment_context_mode: str = "full",
    debug_dir: Optional[Path] = None,
    segment_metrics_override: Optional[set[str]] = None,
    segment_rules_extra: Optional[str] = None,
    period_calendar: str = "calendar",
    document_type: str = "financial_report",
    reporting_standard_override: Optional[str] = None,
    default_unit_override: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """Second-stage segment/geography/product extraction for selected metrics."""
    if effective_type == "bank":
        return

    standard = reporting_standard_override or _detect_reporting_standard(pdf_text_for_prompt)
    default_unit = default_unit_override or _default_unit_for_standard(standard)
    max_report_year = _max_report_anchor_year_for_calendar([period_current, period_prev], period_calendar)
    fiscal_context = FiscalPeriodContext.from_text(pdf_text_for_prompt)
    if segment_metrics_override is not None:
        target_metrics = [m for m in extractable if m.get("enum") in segment_metrics_override]
    else:
        target_metrics = _segment_metric_scope(extractable, standard)
    if not target_metrics:
        return

    deterministic_rows = _deterministic_segment_rows_from_text(pdf_text_for_prompt, standard)
    context_mode = _normalize_segment_context_mode(segment_context_mode)
    context = _build_segment_prompt_context(pdf_text_for_prompt, mode=context_mode)
    if not context.strip() and not deterministic_rows:
        return

    info_blocks = []
    for m in target_metrics:
        enum = m["enum"]
        aliases = [a for a in (m.get("aliases_ru") or []) + (m.get("aliases_en") or []) if a and a != "Нет"]
        block = [
            f"- {enum}: {m.get('name_ru') or ''} / {m.get('name_en') or ''}"
            + (f"; aliases: {'; '.join(aliases[:8])}" if aliases else "")
        ]
        if m.get("description"):
            block.append(f"  description: {m['description']}")
        if m.get("hint"):
            block.append(f"  hint: {m['hint']}")
        info_blocks.append("\n".join(block))

    system_prompt = _financial_analyst_intro_en(
        standard,
        document_type=document_type,
    )
    segment_rules = _segment_rules_en(
        segment_mode,
        document_type=document_type,
    )
    if segment_rules_extra and segment_rules_extra.strip():
        segment_rules = segment_rules + "\n\n" + segment_rules_extra.strip()
    source_rules = _source_specific_actual_rules_en(document_type)

    user_prompt = (
        f"# Company\n{company_display}\n\n"
        f"# Standard\n{standard}\n\n"
        "# Target metrics\n"
        + "\n".join(info_blocks)
        + "\n\n# Period rules\n"
        + _period_rules_en(document_type)
        + "\n\n"
        "# Verbatim value rules\n"
        + _verbatim_value_rules_en(document_type)
        + "\n\n"
        + _actual_values_only_rules_en(document_type)
        + "\n\n"
        + (source_rules + "\n\n" if source_rules else "")
        + "# Segment rules\n"
        + segment_rules
        + "\n\n"
        "Keep TOTAL rows when the table has them. For any "
        "total/summary row, set segment exactly to "
        '"TOTAL" even if the source label says Total net revenue, Total operating '
        "income, Total sales to external customers, Итого or Всего. Preserve signs "
        "for eliminations/corporate rows. If an explicit segment row exists and "
        "a value cell is blank or contains only -, —, – or −, return \"0\" for "
        "that segment value rather than null.\n\n"
        "A single segment table may disclose several target metrics at once. "
        "Do not stop after extracting revenue or profit rows; check the other "
        "rows in the same table against every target metric listed above.\n\n"
        + (
            "# Report excerpts\n"
            if document_type == "financial_report"
            else "# Source excerpts\n"
        )
        + f"{context}\n\n"
        "# Response format\n"
        "Return one valid JSON object only:\n"
        "{\n"
        '  "segments": [\n'
        '    {"enum": "SALES_REVENUE", "segment": "North America", '
        '"dimension": "geography|operating_segment|product|other", '
        '"values": {"Year ended December 31, 2024": "123", '
        '"Year ended December 31, 2023": "111"}, '
        '"unit_text": "in millions", "page": 12}\n'
        "  ]\n"
        "}\n"
    )
    _write_llm_debug_file(debug_dir, "segment_system.md", system_prompt)
    _write_llm_debug_file(debug_dir, "segment_prompt.md", user_prompt)

    import time as _ttiming
    llm_t0 = _ttiming.time()
    try:
        raw, usage = await llm.complete_with_usage(
            prompt=user_prompt,
            system=system_prompt,
            model=model_id,
            response_format={"type": "json_object"},
            max_output=18000,
        )
    except Exception as e:
        yield {"type": "error", "message": f"Segment extraction LLM error: {e}"}
        return
    segment_llm_s = round(_ttiming.time() - llm_t0, 1)
    _write_llm_debug_file(debug_dir, "segment_response.json", raw)

    parsed = _parse_extract_json(raw) or {}
    rows = parsed.get("segments") if isinstance(parsed, dict) else []
    if not isinstance(rows, list):
        rows = []
    rows = [*deterministic_rows, *rows]

    metric_by_enum = {m["enum"]: m for m in target_metrics}
    # Accept US GAAP's common enum name in prompts/GT.
    if "OPERATING_PROFIT" in metric_by_enum:
        metric_by_enum["OPERATING_INCOME"] = metric_by_enum["OPERATING_PROFIT"]

    total_groups: dict[str, list[tuple[dict, list[dict], tuple[tuple[str, str], ...]]]] = {}
    non_total_rows: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        enum = str(row.get("enum") or "").strip().upper()
        metric = metric_by_enum.get(enum)
        if not metric:
            continue
        canonical_enum = metric["enum"]
        segment = str(row.get("segment") or "").strip()
        if not segment:
            continue
        if not _is_total_segment_label(segment):
            non_total_rows.append(row)
            continue
        row = dict(row)
        row["_source_segment"] = segment
        row["segment"] = "TOTAL"
        period_values, _metric_unit = _coerce_period_values(
            row,
            enum=canonical_enum,
            period_current=period_current,
            period_prev=period_prev,
            default_unit=default_unit,
            abs_cost=False,
            dash_as_zero=True,
            max_report_year=max_report_year,
            fiscal_context=fiscal_context,
            period_calendar=period_calendar,
        )
        total_groups.setdefault(canonical_enum, []).append(
            (row, period_values, _period_value_signature(period_values))
        )

    total_diag: list[dict] = []
    for enum, candidates in total_groups.items():
        if len(candidates) == 1:
            non_total_rows.append(candidates[0][0])
            continue
        counts: dict[tuple[tuple[str, str], ...], int] = {}
        for _row, _pvs, sig in candidates:
            counts[sig] = counts.get(sig, 0) + 1
        best_sig = max(counts, key=counts.get)
        best_row = next(row for row, _pvs, sig in candidates if sig == best_sig)
        signatures_disagree = len(counts) > 1
        total_diag.append(
            {
                "enum": enum,
                "candidates": len(candidates),
                "kept_signature_count": counts[best_sig],
                "signatures_disagree": signatures_disagree,
                "source_segments": [str(row.get("_source_segment") or row.get("segment") or "") for row, _pvs, _sig in candidates],
                "signatures": [
                    {"count": count, "values": list(sig)}
                    for sig, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)
                ],
            }
        )
        non_total_rows.append(best_row)
    rows = non_total_rows

    sent = 0
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        enum = str(row.get("enum") or "").strip().upper()
        metric = metric_by_enum.get(enum)
        if not metric:
            continue
        canonical_enum = metric["enum"]
        segment = str(row.get("segment") or "").strip()
        if not segment:
            continue
        dimension = str(row.get("dimension") or "other").strip() or "other"
        period_values, metric_unit = _coerce_period_values(
            row,
            enum=canonical_enum,
            period_current=period_current,
            period_prev=period_prev,
            default_unit=default_unit,
            abs_cost=False,
            dash_as_zero=True,
            max_report_year=max_report_year,
            fiscal_context=fiscal_context,
            period_calendar=period_calendar,
        )
        if not period_values:
            continue
        key = (canonical_enum, segment.lower(), dimension.lower())
        if key in seen:
            continue
        seen.add(key)
        v_cur = _value_for_period(period_values, period_current, period_calendar)
        if v_cur is None:
            v_cur = _first_value(period_values)
        v_prv = _value_for_period(period_values, period_prev, period_calendar)
        yield {
            "type": "metric",
            "enum": canonical_enum,
            "name_ru": metric.get("name_ru") or metric.get("name_en") or canonical_enum,
            "value_current": v_cur,
            "value_prev": v_prv,
            "period_values": period_values,
            "unit": period_values[0].get("unit") or metric_unit,
            "found": any(p.get("value") is not None for p in period_values),
            "is_calculated": False,
            "formula": "",
            "page": _page_from_period_values(period_values),
            "segment": segment,
            "dimension": dimension,
        }
        sent += 1

    if usage:
        from finengine.skills.pricing import cost_usd
        usage_model = str(usage.get("model") or model_id or "")
        tokens_in = int(usage.get("in") or usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("out") or usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        tokens_cached = int(usage.get("cached") or 0)
        yield {
            "type": "segment_diag",
            "_diag_segment": {
                "segment_rows": sent,
                "segment_mode": _normalize_segment_mode(segment_mode),
                "segment_context_mode": context_mode,
                "segment_context_chars": len(context),
                "segment_standard": standard,
                "segment_deterministic_rows": len(deterministic_rows),
                "segment_total_dedupe": total_diag,
                "segment_llm_s": segment_llm_s,
                "segment_tokens_in": tokens_in,
                "segment_tokens_out": tokens_out,
                "segment_tokens_cached": tokens_cached,
                "llm_requests": [
                    {
                        "stage": "segment",
                        "model": usage_model,
                        "elapsed_s": segment_llm_s,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "tokens_cached": tokens_cached,
                        "cost_usd": round(cost_usd(usage_model, tokens_in, tokens_out, tokens_cached), 4),
                    }
                ],
            },
        }


CONSOLIDATED_GROUP_ENUM = "CONSOLIDATED_GROUP_OWNERSHIP"
CONSOLIDATED_GROUP_DIMENSION = "consolidated_group"

_GROUP_ENTITY_KEYWORDS = (
    "компания",
    "компании",
    "наименование",
    "название",
    "организация",
    "общество",
    "дочерняя",
    "subsidiary",
    "subsidiaries",
    "associate",
    "associates",
    "joint venture",
    "entity",
    "company",
    "name",
)
_GROUP_SHARE_KEYWORDS = (
    "доля",
    "доли",
    "участ",
    "владен",
    "собствен",
    "голос",
    "уставном капитале",
    "effective ownership",
    "ownership interest",
    "ownership",
    "voting",
    "shareholding",
    "interest",
)
_GROUP_CONTEXT_KEYWORDS = (
    "дочерн",
    "состав группы",
    "группы",
    "subsidiar",
    "group companies",
    "consolidated group",
)
_GROUP_COMPOSITION_HEADING_KEYWORDS = (
    "основные компании группы",
    "основные компании",
    "состав группы",
    "перечень компаний группы",
    "group companies",
    "principal group companies",
    "main group companies",
    "major group companies",
)
_GROUP_SUBSIDIARY_KEYWORDS = (
    "дочерн",
    "дочерние",
    "дочерних",
    "дочерними",
    "subsidiary",
    "subsidiaries",
    "principal subsidiaries",
    "major subsidiaries",
    "significant subsidiaries",
)
_GROUP_NON_SUBSIDIARY_KEYWORDS = (
    "ассоциир",
    "ассоциирован",
    "ассоциированные",
    "ассоциированных",
    "совместн",
    "совместные",
    "совместных",
    "прочие инвестиции",
    "прочих инвестиций",
    "associate",
    "associates",
    "associated",
    "joint venture",
    "joint ventures",
    "equity method",
    "equity-accounted",
    "other investments",
    "investments in associates",
    "investments in joint",
)
_GROUP_NOT_COMPOSITION_CONTEXT_KEYWORDS = (
    "выбытие дочер",
    "выбытия дочер",
    "приобретение дочер",
    "продажа акций",
    "disposal of subsidiar",
    "disposals of subsidiar",
    "acquisition of subsidiar",
    "sale of subsidiar",
)
_GROUP_NOT_COMPOSITION_TABLE_KEYWORDS = (
    "неконтролирующая доля",
    "неконтролирующей дол",
    "non-controlling interest",
    "капитал, причитающийся акционерам",
    "statement of changes in equity",
    "отчет об изменениях в капитале",
)
_GROUP_EXCLUDE_KEYWORDS = (
    "акционеры",
    "акционер",
    "shareholders",
    "shareholder",
    "дивиденды",
    "dividends",
    "ключевой вопрос аудита",
    "key audit matter",
    "выручка",
    "revenue",
    "сегмент",
    "segment",
)


def _clean_group_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(keyword in low for keyword in keywords)


def _group_percentish_count(text: str) -> int:
    return len(re.findall(r"\b\d{1,3}(?:[,.]\d+)?\s*%", text)) + len(
        re.findall(r"\b100(?:[,.]0+)?\b", text)
    )


def _last_keyword_pos(text: str, keywords: tuple[str, ...]) -> int:
    positions = [text.rfind(keyword) for keyword in keywords]
    return max(positions) if positions else -1


def _group_context_relation_kind(context: str) -> str:
    low = context.lower()
    if _contains_any(low, _GROUP_NOT_COMPOSITION_CONTEXT_KEYWORDS):
        return "not_composition"
    subsidiary_pos = _last_keyword_pos(low, _GROUP_SUBSIDIARY_KEYWORDS)
    non_subsidiary_pos = _last_keyword_pos(low, _GROUP_NON_SUBSIDIARY_KEYWORDS)
    composition_heading_pos = _last_keyword_pos(low, _GROUP_COMPOSITION_HEADING_KEYWORDS)
    if composition_heading_pos > max(subsidiary_pos, non_subsidiary_pos):
        return ""
    if subsidiary_pos < 0 and non_subsidiary_pos < 0:
        return ""
    return "subsidiary" if subsidiary_pos >= non_subsidiary_pos else "non_subsidiary"


def _focused_group_context(context: str, relation_kind: str) -> str:
    low = context.lower()
    keywords = (
        _GROUP_SUBSIDIARY_KEYWORDS
        if relation_kind == "subsidiary"
        else _GROUP_NON_SUBSIDIARY_KEYWORDS
        if relation_kind == "non_subsidiary"
        else _GROUP_NOT_COMPOSITION_CONTEXT_KEYWORDS
        if relation_kind == "not_composition"
        else ()
    )
    pos = _last_keyword_pos(low, keywords) if keywords else -1
    if pos >= 0:
        return context[max(0, pos - 120):].strip()
    return context[-600:].strip()


def _flush_group_table_block(
    blocks: list[dict],
    lines: list[str],
    page: int | None,
    context_lines: list[str] | None = None,
) -> None:
    if len(lines) < 2:
        return
    text = "\n".join(lines).strip()
    compact = _clean_group_text(text)
    if not compact:
        return
    context = _clean_group_text(" ".join(context_lines or []))
    low = compact.lower()
    if _contains_any(low, _GROUP_NOT_COMPOSITION_TABLE_KEYWORDS):
        return
    relation_kind = _group_context_relation_kind(context)
    score = 0
    has_entity = _contains_any(low, _GROUP_ENTITY_KEYWORDS) or bool(
        re.search(r"\b(?:ооо|ао|пао|zao|llc|jsc|pjsc)\b", low, flags=re.IGNORECASE)
    )
    has_share = _contains_any(low, _GROUP_SHARE_KEYWORDS)
    has_period = bool(
        re.search(r"\b(?:20\d{2}|31\s+декабр|30\s+июн|december\s+31|june\s+30)\b", low)
    )
    percent_count = _group_percentish_count(compact)
    has_group_context = _contains_any(low, _GROUP_CONTEXT_KEYWORDS)
    has_subsidiary_context = relation_kind == "subsidiary"
    has_non_subsidiary_context = relation_kind in {"non_subsidiary", "not_composition"}
    has_share_signal = has_share or percent_count >= 2
    if has_entity:
        score += 3
    if has_share:
        score += 5
    if has_period:
        score += 2
    if percent_count:
        score += min(4, percent_count)
    if has_group_context:
        score += 2
    if has_subsidiary_context:
        score += 7
    if has_non_subsidiary_context and not has_subsidiary_context:
        score -= 10
    if _contains_any(low, _GROUP_EXCLUDE_KEYWORDS) and not has_group_context:
        score -= 6
    if has_non_subsidiary_context and not has_subsidiary_context:
        return
    if not has_share_signal or not has_entity or score < 7:
        return
    blocks.append(
        {
            "page": page,
            "score": score,
            "text": text,
            "context": _focused_group_context(context, relation_kind),
            "chars": len(text),
            "subsidiary_context": has_subsidiary_context,
            "non_subsidiary_context": has_non_subsidiary_context,
        }
    )


def _consolidated_group_candidate_tables(
    pdf_text: str,
    *,
    max_tables: int = 24,
) -> list[dict]:
    blocks: list[dict] = []
    buf: list[str] = []
    block_page: int | None = None
    block_context: list[str] = []
    current_page: int | None = None
    recent_text_lines: list[str] = []

    for raw_line in (pdf_text or "").splitlines():
        page_match = re.match(r"\s*===PAGE\s+(\d+)===", raw_line)
        if page_match:
            _flush_group_table_block(blocks, buf, block_page, block_context)
            buf = []
            block_page = None
            block_context = []
            current_page = int(page_match.group(1))
            recent_text_lines = []
            continue
        line = raw_line.rstrip()
        if "|" in line:
            if not buf:
                block_page = current_page
                block_context = list(recent_text_lines[-4:])
            buf.append(line)
            continue
        _flush_group_table_block(blocks, buf, block_page, block_context)
        buf = []
        block_page = None
        block_context = []
        cleaned_line = _clean_group_text(line)
        if cleaned_line:
            recent_text_lines.append(cleaned_line)
            recent_text_lines = recent_text_lines[-8:]
    _flush_group_table_block(blocks, buf, block_page, block_context)

    subsidiary_blocks = [block for block in blocks if block.get("subsidiary_context")]
    if subsidiary_blocks:
        blocks = subsidiary_blocks
    blocks.sort(key=lambda item: (item["score"], item["chars"]), reverse=True)
    return blocks[:max_tables]


def _build_consolidated_group_context(
    pdf_text: str,
    *,
    max_chars: int = 60_000,
    max_tables: int = 24,
) -> tuple[str, list[dict]]:
    candidates = _consolidated_group_candidate_tables(pdf_text, max_tables=max_tables)
    parts: list[str] = []
    used = 0
    for idx, candidate in enumerate(candidates, start=1):
        text = str(candidate.get("text") or "")
        header = (
            f"[Candidate table {idx}; page={candidate.get('page') or 'unknown'}; "
            f"score={candidate.get('score')}]"
        )
        context = str(candidate.get("context") or "").strip()
        context_line = f"\nContext before table: {context}" if context else ""
        chunk = f"{header}{context_line}\n{text}"
        if used + len(chunk) > max_chars and parts:
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n\n".join(parts), candidates[: len(parts)]


def _normalize_group_period_label(raw_period: object) -> str:
    raw = _clean_group_text(raw_period)
    if not raw:
        return ""
    normalized = normalize_period_label(raw)
    if match := re.fullmatch(r"FY\s+(20\d{2})", normalized, flags=re.IGNORECASE):
        return f"31.12.{match.group(1)}"
    if match := re.fullmatch(r"(20\d{2})", raw):
        return f"31.12.{match.group(1)}"
    return normalized


def _normalize_group_share_value(raw_value: object, unit_text: object = "") -> object | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        return f"{raw_value:g}%"
    text = _clean_group_text(raw_value)
    if not text or text.lower() in {"null", "none", "n/a", "нет", "не применимо"}:
        return None
    if text in {"-", "—", "–", "−"}:
        return "-"
    low_unit = str(unit_text or "").lower()
    if "%" not in text and re.fullmatch(r"\d{1,3}(?:[,.]\d+)?", text):
        if "%" in low_unit or "процент" in low_unit or "percent" in low_unit:
            return f"{text}%"
    return text


def _is_plausible_group_share_value(value: object) -> bool:
    if value is None:
        return False
    text = _clean_group_text(value).lower()
    if text in {"-", "—", "–", "−"}:
        return True
    if re.search(r"\d", text) and (
        "%" in text
        or "акци" in text
        or "share" in text
        or re.fullmatch(r"\d{1,3}(?:[,.]\d+)?%?", text)
    ):
        return True
    return False


def _group_value_for_period(period_values: list[dict], target: str) -> object | None:
    target_norm = _normalize_group_period_label(target)
    for item in period_values:
        if _normalize_group_period_label(item.get("period")) == target_norm:
            return item.get("value")
    return None


def _group_row_is_non_subsidiary(row: dict) -> bool:
    relation = _clean_group_text(row.get("relation") or row.get("relationship") or row.get("type")).lower()
    table_type = _clean_group_text(row.get("table_type") or row.get("category")).lower()
    source = _clean_group_text(row.get("source_text") or "").lower()
    probe = " ".join(part for part in (relation, table_type, source) if part)
    if not probe:
        return False
    if _contains_any(probe, _GROUP_NON_SUBSIDIARY_KEYWORDS):
        return True
    return relation in {"associate", "associates", "associated", "joint_venture", "joint venture", "other"}


def _normalise_consolidated_group_rows(
    rows: object,
    *,
    period_current: str,
    period_prev: str,
    candidate_pages: dict[int, int | None] | None = None,
    candidate_allowed: dict[int, bool] | None = None,
) -> list[dict]:
    if not isinstance(rows, list):
        return []
    candidate_pages = candidate_pages or {}
    events_by_company: dict[str, dict] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate_id: int | None = None
        if row.get("candidate_id") is not None:
            try:
                candidate_id = int(row.get("candidate_id"))
            except Exception:
                candidate_id = None
        if candidate_id is not None and candidate_allowed is not None and not candidate_allowed.get(candidate_id, True):
            continue
        if _group_row_is_non_subsidiary(row):
            continue
        name = _clean_group_text(
            row.get("company_name")
            or row.get("company")
            or row.get("name")
            or row.get("entity")
            or row.get("organization")
        )
        if len(name) < 2 or name.lower() in {"итого", "total", "всего"}:
            continue
        values = row.get("values")
        if not isinstance(values, dict):
            continue
        unit_text = row.get("unit_text") or row.get("unit") or "%"
        raw_period_values: list[dict] = []
        for raw_period, raw_value in values.items():
            period = _normalize_group_period_label(raw_period)
            if not period:
                continue
            value = _normalize_group_share_value(raw_value, unit_text)
            if not _is_plausible_group_share_value(value):
                continue
            raw_period_values.append(
                {
                    "period": period,
                    "raw_period": _clean_group_text(raw_period),
                    "value": value,
                    "unit": "%",
                    "page": None,
                    "source_text": _clean_group_text(row.get("source_text") or ""),
                }
            )
        if not raw_period_values:
            continue

        page_raw = row.get("page")
        if not page_raw and candidate_id is not None:
            page_raw = candidate_pages.get(candidate_id)
        try:
            page = int(page_raw) if page_raw is not None else None
        except Exception:
            page = None
        if page is not None and page <= 0:
            page = None
        for pv in raw_period_values:
            pv["page"] = page

        period_values = [
            item for item in raw_period_values if item.get("value") is not None
        ]
        by_period = {
            str(item.get("period") or ""): item
            for item in period_values
            if item.get("period")
        }
        period_values = [by_period[p] for p in sort_periods(list(by_period))]
        value_current = _group_value_for_period(period_values, period_current)
        if value_current is None:
            value_current = _first_value(period_values)
        value_prev = _group_value_for_period(period_values, period_prev)

        source_bits = []
        for key in ("relation", "registration", "activity"):
            value = _clean_group_text(row.get(key))
            if value:
                source_bits.append(f"{key}: {value}")
        source_note = "; ".join(source_bits)
        event = {
            "type": "metric",
            "enum": CONSOLIDATED_GROUP_ENUM,
            "name_ru": name,
            "value_current": value_current,
            "value_prev": value_prev,
            "period_values": period_values,
            "unit": "%",
            "found": True,
            "is_calculated": False,
            "formula": "",
            "page": page,
            "segment": name,
            "dimension": CONSOLIDATED_GROUP_DIMENSION,
            "source_text": source_note,
            "group_relation": _clean_group_text(row.get("relation")),
            "group_registration": _clean_group_text(row.get("registration")),
            "group_activity": _clean_group_text(row.get("activity")),
        }

        key = name.lower()
        existing = events_by_company.get(key)
        if not existing:
            events_by_company[key] = event
            continue
        merged: dict[str, dict] = {
            str(item.get("period") or ""): item
            for item in existing.get("period_values") or []
            if item.get("period")
        }
        for item in period_values:
            period = str(item.get("period") or "")
            if period and period not in merged:
                merged[period] = item
        existing["period_values"] = [
            merged[p] for p in sort_periods(list(merged.keys()))
        ]
        existing["value_current"] = (
            _group_value_for_period(existing["period_values"], period_current)
            or _first_value(existing["period_values"])
        )
        existing["value_prev"] = _group_value_for_period(existing["period_values"], period_prev)
        if not existing.get("page") and page:
            existing["page"] = page

    return sorted(events_by_company.values(), key=lambda item: str(item.get("name_ru") or "").lower())


async def _extract_consolidated_group_full_context(
    *,
    pdf_text_for_prompt: str,
    period_current: str,
    period_prev: str,
    company_display: str,
    reporting_standard: str,
    company_type: str,
    model_id: Optional[str],
    debug_dir: Optional[Path] = None,
) -> AsyncGenerator[dict, None]:
    """Extract group composition / ownership rows for the generic role."""
    context, candidates = _build_consolidated_group_context(pdf_text_for_prompt)
    diag_base = {
        "consolidated_group_candidate_tables": len(candidates),
        "consolidated_group_rows": 0,
        "consolidated_group_company_type": company_type,
    }
    if not candidates or not context.strip():
        _LOG.info(
            "Consolidated group stage skipped for %s (%s): no candidate tables",
            company_display,
            company_type,
        )
        yield {
            "type": "consolidated_group_diag",
            "_diag_consolidated_group": {
                **diag_base,
                "consolidated_group_skip_reason": "no_candidate_tables",
            },
        }
        return

    candidate_pages = {
        idx: candidate.get("page")
        for idx, candidate in enumerate(candidates, start=1)
    }
    candidate_allowed = {
        idx: not bool(candidate.get("non_subsidiary_context"))
        for idx, candidate in enumerate(candidates, start=1)
    }
    system_prompt = (
        "You extract only consolidated group composition tables from financial "
        "reports. You are careful, literal, and return JSON only."
    )
    user_prompt = (
        f"# Company\n{company_display}\n\n"
        f"# Reporting standard\n{reporting_standard}\n\n"
        "# Task\n"
        "Find ONLY subsidiaries / дочерние общества (ДЗО) that form the "
        "consolidated group and are included in consolidation. Extract "
        "ownership/effective interest/voting share percentages by period.\n\n"
        "# Table selection\n"
        "- Use only the single table whose heading/context says subsidiaries, "
        "дочерние общества, дочерние предприятия, or principal/significant subsidiaries.\n"
        "- If several candidate tables are shown, choose that one subsidiaries table "
        "and ignore all other candidate tables.\n"
        "- If the candidates do not contain a subsidiaries table, return "
        '{"companies": []}.\n\n'
        "# Strict exclusions\n"
        "- Do NOT extract shareholders of the parent company.\n"
        "- Do NOT extract associates / ассоциированные общества.\n"
        "- Do NOT extract joint ventures / совместные предприятия.\n"
        "- Do NOT extract equity-accounted investees or other investments.\n"
        "- Do NOT extract operating segments, revenue structure, related-party "
        "transactions, investments, auditors' key matters, or financial metrics.\n"
        "# Value rules\n"
        "- Prefer ownership/effective interest/shareholding over voting rights.\n"
        "- Use voting rights only if ownership/effective interest is absent.\n"
        "- Preserve literal values: `100,00%`, `75% минус одна акция`, `-`.\n"
        "- If a value is written as `100` in a percentage column, return `100%`.\n"
        "- Keep period headers literally as written in the table.\n\n"
        "# Candidate tables\n"
        f"{context}\n\n"
        "# Response format\n"
        "Return one valid JSON object only:\n"
        "{\n"
        '  "companies": [\n'
        "    {\n"
        '      "candidate_id": 1,\n'
        '      "company_name": "ООО «Example»",\n'
        '      "relation": "subsidiary|unknown",\n'
        '      "registration": "Москва, РФ",\n'
        '      "activity": "Воздушные перевозки",\n'
        '      "values": {"31 декабря 2024 г.": "100,00%"},\n'
        '      "unit_text": "%",\n'
        '      "page": 17,\n'
        '      "source_text": "short source row excerpt"\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )
    _write_llm_debug_file(debug_dir, "consolidated_group_system.md", system_prompt)
    _write_llm_debug_file(debug_dir, "consolidated_group_prompt.md", user_prompt)

    import time as _ttiming
    llm_t0 = _ttiming.time()
    _LOG.info(
        "Consolidated group stage started for %s (%s): %s candidate tables",
        company_display,
        company_type,
        len(candidates),
    )
    try:
        raw, usage = await llm.complete_with_usage(
            prompt=user_prompt,
            system=system_prompt,
            model=model_id,
            response_format={"type": "json_object"},
            max_output=18000,
        )
    except Exception as e:
        _LOG.exception(
            "Consolidated group stage failed for %s (%s)",
            company_display,
            company_type,
        )
        yield {
            "type": "consolidated_group_diag",
            "_diag_consolidated_group": {
                **diag_base,
                "consolidated_group_error": f"{type(e).__name__}: {e}"[:300],
            },
        }
        return

    group_llm_s = round(_ttiming.time() - llm_t0, 1)
    _write_llm_debug_file(debug_dir, "consolidated_group_response.json", raw)
    parsed = _parse_extract_json(raw) or {}
    rows = parsed.get("companies") if isinstance(parsed, dict) else []
    events = _normalise_consolidated_group_rows(
        rows,
        period_current=period_current,
        period_prev=period_prev,
        candidate_pages=candidate_pages,
        candidate_allowed=candidate_allowed,
    )
    for event in events:
        yield event
    _LOG.info(
        "Consolidated group stage finished for %s (%s): %s rows from %s candidates in %.1fs",
        company_display,
        company_type,
        len(events),
        len(candidates),
        group_llm_s,
    )

    diag = {
        **diag_base,
        "consolidated_group_rows": len(events),
        "consolidated_group_llm_s": group_llm_s,
    }
    if usage:
        from finengine.skills.pricing import cost_usd

        usage_model = str(usage.get("model") or model_id or "")
        tokens_in = int(
            usage.get("in")
            or usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or 0
        )
        tokens_out = int(
            usage.get("out")
            or usage.get("completion_tokens")
            or usage.get("output_tokens")
            or 0
        )
        tokens_cached = int(usage.get("cached") or 0)
        diag.update(
            {
                "consolidated_group_tokens_in": tokens_in,
                "consolidated_group_tokens_out": tokens_out,
                "consolidated_group_tokens_cached": tokens_cached,
                "llm_requests": [
                    {
                        "stage": "consolidated_group",
                        "model": usage_model,
                        "elapsed_s": group_llm_s,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "tokens_cached": tokens_cached,
                        "cost_usd": round(
                            cost_usd(usage_model, tokens_in, tokens_out, tokens_cached),
                            4,
                        ),
                    }
                ],
            }
        )
    yield {
        "type": "consolidated_group_diag",
        "_diag_consolidated_group": diag,
    }


_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9_]*\b")
_NON_METRIC_DEPS = {"AVG", "ABS", "MIN", "MAX"}
_OPTIONAL_ZERO_DEPS_BY_ENUM = {
    "TOTAL_CASH_EQUIVALENTS": {"BANK_DEPOSITS"},
    "NET_DEBT": {"BANK_DEPOSITS"},
}
_PARTIAL_ZERO_FORMULA_ENUMS = {"CAPEX", "DIVIDENDS", "TOTAL_DEBT"}


class _MissingFormulaInput(Exception):
    """Raised when a calculated formula cannot be evaluated for this period."""


def _formula_metric_tokens(formula: str) -> set[str]:
    return set(_TOKEN_RE.findall(_normalise_formula_expr(formula))) - _NON_METRIC_DEPS


def _numeric_env_keys(
    extracted: dict[str, Optional[float]],
    calculated: dict[str, Optional[float]] | None = None,
) -> set[str]:
    keys: set[str] = set()
    for source in (calculated or {}, extracted):
        for key, value in source.items():
            if value is None:
                continue
            try:
                float(value)
            except Exception:
                continue
            keys.add(str(key))
    return keys


def _formula_allows_partial_zero(formula: str) -> bool:
    expr = _normalise_formula_expr(formula)
    if "/" in expr or "*" in expr:
        return False
    return not re.search(r"\b(?:AVG|MIN|MAX)\s*\(", expr, re.I)


def _optional_zero_deps_for_formula(
    enum: str,
    formula: str,
    extracted: dict[str, Optional[float]],
    calculated: dict[str, Optional[float]] | None = None,
) -> set[str]:
    optional_zero_deps = set(_OPTIONAL_ZERO_DEPS_BY_ENUM.get(enum, set()))
    if enum not in _PARTIAL_ZERO_FORMULA_ENUMS:
        return optional_zero_deps
    tokens = _formula_metric_tokens(formula)
    if not tokens or not _formula_allows_partial_zero(formula):
        return optional_zero_deps
    present = _numeric_env_keys(extracted, calculated) & tokens
    if present:
        optional_zero_deps.update(tokens - present)
    return optional_zero_deps


def _formula_env(
    extracted: dict[str, Optional[float]],
    calculated: dict[str, Optional[float]] | None = None,
    *,
    optional_zero_deps: set[str] | None = None,
    formula_default_abs_enums: set[str] | None = None,
) -> dict[str, float]:
    env: dict[str, float] = {}
    for key, value in (calculated or {}).items():
        if value is None:
            continue
        try:
            env[str(key)] = float(value)
        except Exception:
            continue
    default_abs = formula_default_abs_enums or set()
    for key, value in extracted.items():
        if value is None:
            continue
        try:
            numeric = float(value)
        except Exception:
            continue
        if str(key) in default_abs:
            numeric = abs(numeric)
        env[str(key)] = numeric
    for key in optional_zero_deps or set():
        env.setdefault(key, 0.0)
    return env


def _normalise_formula_expr(expr: str) -> str:
    normalised = (
        expr.replace("×", "*")
        .replace("÷", "/")
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace("%", "")
    )
    return re.sub(r"(?<=\d),(?=\d)", ".", normalised)


def _eval_formula_expr(expr: str, env: dict[str, float]) -> float:
    expr = _normalise_formula_expr(expr)
    for token in sorted(set(_TOKEN_RE.findall(expr)), key=len, reverse=True):
        if token in _NON_METRIC_DEPS:
            continue
        if token not in env:
            raise _MissingFormulaInput(token)
        expr = re.sub(r"\b" + re.escape(token) + r"\b", str(env[token]), expr)
    safe_functions = {"ABS": abs, "MIN": min, "MAX": max}
    return float(eval(expr, {"__builtins__": {}}, safe_functions))  # noqa: S307 — controlled formula


_POINT_PERIOD_RE = re.compile(r"^(\d{1,2})[.](\d{1,2})[.](20\d{2})$")


def _point_period_key(period: object) -> tuple[int, int, int] | None:
    """Return sortable key only for normalized point-in-time date labels."""
    p = normalize_period_label(period)
    m = _POINT_PERIOD_RE.fullmatch(p)
    if not m:
        return None
    day, month, year = map(int, m.groups())
    return year, month, day


def _period_end_key(period: object, period_calendar: str = "calendar") -> tuple[int, int, int] | None:
    """Best-effort period end date for matching duration rows to point rows."""
    if _is_indian_fiscal_calendar(period_calendar):
        return _indian_fiscal_period_end_key(period)
    p = normalize_period_label(period)
    point_key = _point_period_key(p)
    if point_key is not None:
        return point_key
    m = re.fullmatch(r"FY (20\d{2})", p)
    if m:
        return int(m.group(1)), 12, 31
    m = re.fullmatch(r"Q([1-4]) (20\d{2})", p)
    if m:
        q = int(m.group(1))
        month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[q]
        return int(m.group(2)), month_day[0], month_day[1]
    m = re.fullmatch(r"([69])M (20\d{2})", p)
    if m:
        months = int(m.group(1))
        return int(m.group(2)), months, 30
    m = re.fullmatch(r"\d{1,2}M (\d{1,2})[.](\d{1,2})[.](20\d{2})", p)
    if m:
        day, month, year = map(int, m.groups())
        return year, month, day
    return None


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in {4, 6, 9, 11} else 31


def _subtract_months_key(key: tuple[int, int, int], months: int) -> tuple[int, int, int]:
    year, month, day = key
    source_is_month_end = day == _days_in_month(year, month)
    total_month = year * 12 + (month - 1) - months
    target_year = total_month // 12
    target_month = total_month % 12 + 1
    target_month_days = _days_in_month(target_year, target_month)
    target_day = target_month_days if source_is_month_end else min(day, target_month_days)
    return target_year, target_month, target_day


def _period_boundary_keys(
    period: object,
    period_calendar: str = "calendar",
) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    """Return exact beginning/end point dates for normalized calendar periods.

    AVG(point metric) must average the point values at the flow period
    boundaries, not merely the two latest balance-sheet dates before the flow
    period end. For example, 6M 2025 starts at 31.12.2024 and ends at
    30.06.2025; 31.03.2025 is an intermediate point and must not be used.
    """
    p = normalize_period_label(period)
    if _is_indian_fiscal_calendar(period_calendar):
        if m := re.fullmatch(r"FY (20\d{2})", p):
            start_year = int(m.group(1))
            return (start_year, 3, 31), (start_year + 1, 3, 31)
        if m := re.fullmatch(r"Q([1-4]) (20\d{2})", p):
            quarter = int(m.group(1))
            start_year = int(m.group(2))
            start = {
                1: (start_year, 3, 31),
                2: (start_year, 6, 30),
                3: (start_year, 9, 30),
                4: (start_year, 12, 31),
            }[quarter]
            end = {
                1: (start_year, 6, 30),
                2: (start_year, 9, 30),
                3: (start_year, 12, 31),
                4: (start_year + 1, 3, 31),
            }[quarter]
            return start, end
        if m := re.fullmatch(r"([69])M (20\d{2})", p):
            months = int(m.group(1))
            start_year = int(m.group(2))
            end = {6: (start_year, 9, 30), 9: (start_year, 12, 31)}[months]
            return (start_year, 3, 31), end
        if m := re.fullmatch(r"12M (20\d{2})", p):
            start_year = int(m.group(1))
            return (start_year, 3, 31), (start_year + 1, 3, 31)

    m = re.fullmatch(r"FY (20\d{2})", p)
    if m:
        year = int(m.group(1))
        return (year - 1, 12, 31), (year, 12, 31)
    m = re.fullmatch(r"Q([1-4]) (20\d{2})", p)
    if m:
        q = int(m.group(1))
        year = int(m.group(2))
        start_month_day = {
            1: (12, 31),
            2: (3, 31),
            3: (6, 30),
            4: (9, 30),
        }[q]
        end_month_day = {
            1: (3, 31),
            2: (6, 30),
            3: (9, 30),
            4: (12, 31),
        }[q]
        start_year = year - 1 if q == 1 else year
        return (start_year, start_month_day[0], start_month_day[1]), (
            year,
            end_month_day[0],
            end_month_day[1],
        )
    m = re.fullmatch(r"([69])M (20\d{2})", p)
    if m:
        months = int(m.group(1))
        year = int(m.group(2))
        return (year - 1, 12, 31), (year, months, 30)
    m = re.fullmatch(r"(\d{1,2})M (\d{1,2})[.](\d{1,2})[.](20\d{2})", p)
    if m:
        months, day, month, year = map(int, m.groups())
        end_key = (year, month, day)
        return _subtract_months_key(end_key, months), end_key
    return None


def _with_point_values_for_duration_calculations(
    extracted_by_period: dict[str, dict[str, Optional[float]]],
    periods: list[str],
    period_calendar: str = "calendar",
) -> dict[str, dict[str, Optional[float]]]:
    """Expose point-in-time values to duration formulas with the same end date.

    This is intentionally calculation-only. The original period_values stay on
    their source date so UI/export code can decide a single display column.
    """
    out = {period: dict(values) for period, values in extracted_by_period.items()}
    point_values: list[tuple[tuple[int, int, int], str, Optional[float]]] = []
    for period, values in extracted_by_period.items():
        point_key = _point_period_key(period)
        if point_key is None:
            continue
        for enum, value in values.items():
            if period_kind_for_enum(enum) == POINT_KIND:
                point_values.append((point_key, enum, value))

    if not point_values:
        return out

    for period in periods:
        if _point_period_key(period) is not None:
            continue
        end_key = _period_end_key(period, period_calendar)
        if end_key is None:
            continue
        target = out.setdefault(period, {})
        for point_key, enum, value in point_values:
            if point_key == end_key:
                target.setdefault(enum, value)
    return out


def _calculated_period_kind_map(calc_defs: list) -> dict[str, str]:
    """Infer whether calculated metrics are point-like or duration-like."""
    calc_by_enum = {str(enum).strip().upper(): (formula, deps) for enum, _name, formula, deps in calc_defs}
    memo: dict[str, str] = {}
    visiting: set[str] = set()

    def kind_for(enum: object) -> str:
        key = str(enum or "").strip().upper()
        if not key:
            return DURATION_KIND
        if key in memo:
            return memo[key]
        if key not in calc_by_enum:
            return period_kind_for_enum(key)
        if key in visiting:
            return DURATION_KIND

        visiting.add(key)
        _formula, deps = calc_by_enum[key]
        dep_kinds = [
            kind_for(dep)
            for dep in deps
            if str(dep or "").strip().upper() not in _NON_METRIC_DEPS
        ]
        visiting.remove(key)
        memo[key] = POINT_KIND if dep_kinds and all(dep_kind == POINT_KIND for dep_kind in dep_kinds) else DURATION_KIND
        return memo[key]

    for enum in calc_by_enum:
        kind_for(enum)
    return memo


def _env_from_period_values(
    extracted_by_period: dict[str, dict[str, Optional[float]]],
    calculated_by_period: dict[str, dict[str, Optional[float]]],
    period: str,
    formula_default_abs_enums: set[str] | None = None,
) -> dict[str, float]:
    env: dict[str, float] = {}
    for key, value in calculated_by_period.get(period, {}).items():
        if value is None:
            continue
        try:
            env[str(key)] = float(value)
        except Exception:
            continue
    default_abs = formula_default_abs_enums or set()
    for key, value in extracted_by_period.get(period, {}).items():
        if value is None:
            continue
        try:
            numeric = float(value)
        except Exception:
            continue
        if str(key) in default_abs:
            numeric = abs(numeric)
        env[str(key)] = numeric
    return env


def _build_avg_resolver_for_period(
    target_period: str,
    extracted_by_period: dict[str, dict[str, Optional[float]]],
    calculated_by_period: dict[str, dict[str, Optional[float]]],
    point_periods: list[str],
    formula_default_abs_enums: set[str] | None = None,
    period_calendar: str = "calendar",
) -> Callable[[str], float] | None:
    target_key = _period_end_key(target_period, period_calendar)
    if target_key is None:
        return None
    ordered_points = sorted(
        ((p, _point_period_key(p)) for p in point_periods),
        key=lambda item: item[1] or (0, 0, 0),
    )
    if not ordered_points:
        return None

    def resolve(inner_expr: str) -> float:
        boundary_keys = _period_boundary_keys(target_period, period_calendar)
        if boundary_keys is not None:
            values: list[float] = []
            for boundary_key in boundary_keys:
                point_period = next(
                    (
                        p
                        for p, point_key in ordered_points
                        if point_key == boundary_key
                    ),
                    None,
                )
                if point_period is None:
                    raise _MissingFormulaInput("AVG boundary point periods")
                env = _env_from_period_values(
                    extracted_by_period,
                    calculated_by_period,
                    point_period,
                    formula_default_abs_enums,
                )
                values.append(_eval_formula_expr(inner_expr, env))
            return (values[0] + values[1]) / 2.0

        values: list[float] = []
        for point_period, point_key in ordered_points:
            if point_key is None or point_key > target_key:
                continue
            env = _env_from_period_values(
                extracted_by_period,
                calculated_by_period,
                point_period,
                formula_default_abs_enums,
            )
            try:
                values.append(_eval_formula_expr(inner_expr, env))
            except Exception:
                continue
        if len(values) < 2:
            raise _MissingFormulaInput("AVG point periods")
        current_value = values[-1]
        previous_value = values[-2]
        return (current_value + previous_value) / 2.0

    return resolve


def _replace_avg_calls(
    formula: str,
    current_env: dict[str, float],
    previous_env: dict[str, float] | None,
    avg_resolver: Callable[[str], float] | None = None,
) -> str:
    """Replace only scoped AVG(...) calls with their current/previous average."""
    result: list[str] = []
    pos = 0
    while pos < len(formula):
        match = re.search(r"\bAVG\s*\(", formula[pos:], re.I)
        if not match:
            result.append(formula[pos:])
            break

        start = pos + match.start()
        open_paren = pos + match.end() - 1
        result.append(formula[pos:start])

        depth = 1
        end = open_paren + 1
        while end < len(formula) and depth:
            if formula[end] == "(":
                depth += 1
            elif formula[end] == ")":
                depth -= 1
            end += 1
        inner = formula[open_paren + 1 : end - 1].strip()
        if depth:
            raise _MissingFormulaInput("AVG closing parenthesis")
        if avg_resolver is not None:
            result.append(str(avg_resolver(inner)))
        else:
            if previous_env is None:
                raise _MissingFormulaInput("AVG previous period")
            current_value = _eval_formula_expr(inner, current_env)
            previous_value = _eval_formula_expr(inner, previous_env)
            result.append(str((current_value + previous_value) / 2.0))
        pos = end
    return "".join(result)


def _eval_calculated_formula(
    formula: str,
    extracted: dict[str, Optional[float]],
    calculated: dict[str, Optional[float]],
    previous_extracted: dict[str, Optional[float]] | None,
    *,
    previous_calculated: dict[str, Optional[float]] | None = None,
    optional_zero_deps: set[str] | None = None,
    avg_resolver: Callable[[str], float] | None = None,
    formula_default_abs_enums: set[str] | None = None,
) -> float:
    current_env = _formula_env(
        extracted,
        calculated,
        optional_zero_deps=optional_zero_deps,
        formula_default_abs_enums=formula_default_abs_enums,
    )
    previous_env = (
        _formula_env(
            previous_extracted or {},
            previous_calculated,
            optional_zero_deps=optional_zero_deps,
            formula_default_abs_enums=formula_default_abs_enums,
        )
        if previous_extracted is not None or previous_calculated is not None
        else None
    )

    expr = _replace_avg_calls(formula, current_env, previous_env, avg_resolver)
    return _eval_formula_expr(expr, current_env)


def _calculate_metrics(
    calc_defs: list,
    extracted: dict[str, Optional[float]],
    previous_extracted: dict[str, Optional[float]] | None = None,
    avg_resolver: Callable[[str], float] | None = None,
    *,
    previous_calculated: dict[str, Optional[float]] | None = None,
    formula_default_abs_enums: set[str] | None = None,
) -> dict[str, Optional[float]]:
    """Eval calculated formulas from the registry against extracted spot values.

    Two non-trivial pre-processing steps before eval, fixing legacy formula
    syntax that doesn't parse as Python:

    1. `* 100%` → `* 100`. The trailing `%` is editorial («процент»), not the
       Python modulo operator. Without this strip every *_MARGIN formula
       (EBITDA_MARGIN, NET_INCOME_MARGIN, OPERATING_MARGIN, ROA с `%`, …)
       throws SyntaxError silently and returns None. На Лукойле 2021 это
       проявлялось как «маржа по EBITDA не посчитана» в цепочках 1, 17.

    2. `AVG(X)` is evaluated from point-in-time date rows when period_values are
       available: evaluate X on the latest date at or before the target period
       end and on the previous date where X is computable. Legacy current/prev
       callers without date rows keep the old two-env fallback.
    """
    result: dict[str, Optional[float]] = {}
    for _ in range(3):  # multi-pass for chained dependencies
        for enum, _name, formula, deps in calc_defs:
            if result.get(enum) is not None:
                continue
            optional_zero_deps = _optional_zero_deps_for_formula(
                enum,
                formula,
                extracted,
                result,
            )
            try:
                result[enum] = _eval_calculated_formula(
                    formula,
                    extracted,
                    result,
                    previous_extracted,
                    previous_calculated=previous_calculated,
                    optional_zero_deps=optional_zero_deps,
                    avg_resolver=avg_resolver,
                    formula_default_abs_enums=formula_default_abs_enums,
                )
            except Exception:
                result[enum] = None

    # ── Targeted fallbacks for headline metrics ────────────────────────────
    # Реестр определяет EBITDA как OPERATING_PROFIT + DEPRECIATION_AMORTIZATION.
    # На Лукойле 2021 (run-3 в нашей валидации) экстрактор пропустил
    # OPERATING_PROFIT — реранкер не вытащил стр. 9 ОПУ — и в результате
    # EBITDA не считалась, а зависимые цепочки 1, 2, 19 говорили «данных
    # нет». При этом NET_INCOME, EBT, INTEREST_PAID и D&A были извлечены.
    # Алгебраически EBITDA = EBT + |INTEREST_PAID| + D&A — bottom-up через
    # ОПУ, корректный fallback. Срабатывает только когда основная формула
    # не дала результата.
    if result.get("EBITDA") is None:
        ebt = extracted.get("EBT")
        interest = extracted.get("INTEREST_PAID") or extracted.get("FINANCIAL_EXPENSES")
        depam = extracted.get("DEPRECIATION_AMORTIZATION")
        if ebt is not None and interest is not None and depam is not None:
            try:
                result["EBITDA"] = float(ebt) + abs(float(interest)) + float(depam)
            except Exception:
                pass
        # Дополнительный проход для метрик, зависящих от EBITDA
        # (EBITDA_MARGIN, RATIO_NET_DEBT_EBITDA, RATIO_TOTAL_DEBT_EBITDA, …):
        # они уже отметились как None в основном цикле, нужно перевычислить
        # с учётом fallback-EBITDA.
        if result.get("EBITDA") is not None:
            for enum, _name, formula, deps in calc_defs:
                if result.get(enum) is not None or "EBITDA" not in deps:
                    continue
                try:
                    optional_zero_deps = _optional_zero_deps_for_formula(
                        enum,
                        formula,
                        extracted,
                        result,
                    )
                    result[enum] = _eval_calculated_formula(
                        formula,
                        extracted,
                        result,
                        previous_extracted,
                        previous_calculated=previous_calculated,
                        optional_zero_deps=optional_zero_deps,
                        avg_resolver=avg_resolver,
                        formula_default_abs_enums=formula_default_abs_enums,
                    )
                except Exception:
                    pass

    return result


def _calculate_metrics_for_periods(
    calc_defs: list,
    extracted_by_period: dict[str, dict[str, Optional[float]]],
    periods: list[str],
    formula_default_abs_enums: set[str] | None = None,
    period_calendar: str = "calendar",
) -> dict[str, dict[str, Optional[float]]]:
    """Calculate metrics per period, resolving AVG from point-in-time rows.

    ``sort_periods`` returns newest → oldest, so we iterate oldest → newest.
    That lets calculated point metrics exist before later duration metrics call
    `AVG(CALCULATED_POINT_METRIC)`.
    """
    ordered = sort_periods(periods)
    result_by_period: dict[str, dict[str, Optional[float]]] = {}
    point_periods = sorted(
        {p for p in extracted_by_period if _point_period_key(p)},
        key=period_sort_key,
    )
    for idx in range(len(ordered) - 1, -1, -1):
        period = ordered[idx]
        previous_period = ordered[idx + 1] if idx + 1 < len(ordered) else None
        previous_extracted = None
        previous_calculated = None
        if previous_period is not None:
            previous_extracted = extracted_by_period.get(previous_period, {})
            previous_calculated = result_by_period.get(previous_period, {})
        avg_resolver = _build_avg_resolver_for_period(
            period,
            extracted_by_period,
            result_by_period,
            point_periods,
            formula_default_abs_enums,
            period_calendar=period_calendar,
        )
        result_by_period[period] = _calculate_metrics(
            calc_defs,
            extracted_by_period.get(period, {}),
            previous_extracted,
            avg_resolver,
            previous_calculated=previous_calculated,
            formula_default_abs_enums=formula_default_abs_enums,
        )
    return {period: result_by_period.get(period, {}) for period in ordered}


def _calculate_metrics_pair(
    calc_defs: list,
    extracted_current: dict[str, Optional[float]],
    extracted_prev: dict[str, Optional[float]],
    formula_default_abs_enums: set[str] | None = None,
) -> tuple[dict[str, Optional[float]], dict[str, Optional[float]]]:
    """Compatibility helper for legacy current/previous-only callers."""
    calc_prev = _calculate_metrics(
        calc_defs,
        extracted_prev,
        formula_default_abs_enums=formula_default_abs_enums,
    )
    calc_current = _calculate_metrics(
        calc_defs,
        extracted_current,
        extracted_prev,
        previous_calculated=calc_prev,
        formula_default_abs_enums=formula_default_abs_enums,
    )
    return calc_current, calc_prev


# ─────────────────────────────────────────────────────────────────────────────
# Chunk retrieval — multi-stage (validator.py approach)
# ─────────────────────────────────────────────────────────────────────────────

def _retrieve_chunks_for_metric(
    metric: dict,
    index,
    doc_id: str,
    fin_table_chunks: list,
    max_context_chunks: int = 50,
) -> list:
    """Multi-stage chunk retrieval for a single metric."""

    class _Chunk:
        __slots__ = ("content", "metadata", "chunk_type")

        def __init__(self, content, metadata, chunk_type):
            self.content = content
            self.metadata = metadata
            self.chunk_type = chunk_type

    seen: set = set()
    chunks: list[_Chunk] = []

    def _add_search(query: str, top_k: int = 15, min_score: float = 0.15):
        for item in index.search(query, top_k=top_k, doc_ids=[doc_id], min_score=min_score):
            cid = item.metadata.get("chunk_id", id(item))
            if cid not in seen:
                seen.add(cid)
                chunks.append(_Chunk(item.content, item.metadata, item.chunk_type))

    enum = metric["enum"]

    # Step 1: pinpoint page expansion
    pinpoint_aliases = METRIC_EXPANSION_ALIASES.get(enum, [])
    extra_pages: set[int] = set()
    for alias in pinpoint_aliases:
        for r in index.search(alias, top_k=2, doc_ids=[doc_id], min_score=0.625):
            pg = r.metadata.get("page")
            if pg is not None:
                extra_pages.add(pg)

    if extra_pages:
        for chunk in index._chunks:
            if chunk.doc_id == doc_id and chunk.metadata.get("page") in extra_pages:
                cid = chunk.chunk_id
                if cid not in seen:
                    seen.add(cid)
                    chunks.append(_Chunk(chunk.content, chunk.metadata, chunk.chunk_type))

    # Step 2: curated extra aliases
    for alias in METRIC_ALIASES_EXTRA.get(enum, []):
        _add_search(alias)

    # Step 3: all financial_table pages (up to 12 — most important)
    ft_sorted = sorted(fin_table_chunks, key=lambda x: x.metadata.get("page", 999))
    for item in ft_sorted[:12]:
        cid = item.metadata.get("chunk_id", id(item))
        if cid not in seen:
            seen.add(cid)
            chunks.append(_Chunk(item.content, item.metadata, item.chunk_type))

    # Step 4: standard name/alias FAISS search
    _add_search(metric["name_ru"])
    if metric.get("name_en"):
        _add_search(metric["name_en"])

    return chunks[:max_context_chunks]


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_metric_prompt(
    metric: dict,
    chunks: list,
    period_current: str,
    period_prev: str,
) -> str:
    context = "\n\n".join([
        f"[стр. {c.metadata.get('page', '?')} | {c.chunk_type}]\n{c.content}"
        for c in chunks[:50]
    ])

    enum = metric["enum"]
    name_ru = metric["name_ru"]
    name_en = metric.get("name_en", "")
    is_cost = metric.get("is_cost", False) or enum in _COST_METRIC_CODES

    aliases_hint = ""
    extra_aliases = METRIC_ALIASES_EXTRA.get(enum, [])
    if extra_aliases:
        aliases_hint = f"\nДругие названия: {'; '.join(extra_aliases[:5])}"

    name_en_hint = f" / {name_en}" if name_en else ""

    sign_rule = (
        "\n⚠️ ЗНАК: сохраняй знак/скобки как в источнике. "
        "Если в ячейке стоит (102 771) или -102 771, верни именно это представление, "
        "не абсолютное положительное число. Парсер сам превратит скобки в отрицательное число."
        if is_cost else
        "\nЧисла в скобках (1 234) означают отрицательное число (убыток/долг)."
    )

    extra_hint = METRIC_PROMPT_HINTS.get(enum, "")

    # Column hint: first column = current year, second = previous
    col_hint = (
        f" В таблице обычно два числовых столбца: ПЕРВОЕ число = {period_current} (текущий), "
        f"ВТОРОЕ = {period_prev} (предыдущий). "
        f"Число 1–50 перед суммой = ссылка на примечание, пропусти."
    )

    return f"""Извлеки числовое значение показателя из фрагментов финансовой отчётности.

ЗАДАЧА: найти «{name_ru}» [{enum}{name_en_hint}] за ДВА периода: текущий ({period_current}) и предыдущий ({period_prev}).{sign_rule}{aliases_hint}{extra_hint}

ФРАГМЕНТЫ:
{context}

ПРАВИЛА:
1. Найди строку с названием показателя (или синонима из «Другие названия»).
2.{col_hint}
3. Формат: «1 021 976» → 1021976 (пробелы = тысячи).
4. Данные консолидированные (ГРУППЫ), не отдельного предприятия.
5. Таблица движения (rollforward) с колонками по категориям (Итого): бери «Итого» за целевую дату.
6. ⚠️ ШКАЛА — критически важно. Возвращай число РОВНО как оно стоит в таблице,
   НЕ умножай и НЕ конвертируй. Шкалу укажи в поле «единица» — посмотри в
   шапке таблицы / подзаголовке / сноске («в млрд руб.», «в млн руб.», «тыс. руб.»,
   «%», «шт.», «тонн»). Если в данной таблице шкала «в миллиардах», верни
   60855.1 и единицу «млрд руб.» — НЕ 60855100.

ОТВЕТ строго в формате (ровно три строки):
текущий: <число или "не найдено">
предыдущий: <число или "не найдено">
единица: <как в таблице, например «млрд руб.» / «млн руб.» / «%» / «шт.»>

Ответ:"""


# ─────────────────────────────────────────────────────────────────────────────
# OpenRouter call (single-turn)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_extract_json(s: str) -> dict | None:
    """Robust JSON parser for full-context extract responses.

    Tries: (1) raw, (2) markdown-fence-stripped, (3) largest balanced {…} block.
    Returns None if no dict can be recovered. Used because models sometimes
    wrap output in ```json … ``` despite response_format=json_object hints.
    """
    import json as _json
    import re as _re
    s = (s or "").strip()
    if not s:
        return None
    s2 = _re.sub(r"^```(?:json)?\s*", "", s)
    s2 = _re.sub(r"\s*```$", "", s2)
    for cand in (s, s2):
        try:
            obj = _json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    # Largest balanced {…}
    depth = 0
    start = -1
    best = None
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                blk = s[start:i + 1]
                if best is None or len(blk) > len(best):
                    best = blk
    if best:
        try:
            obj = _json.loads(best)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


async def _call_claude_metric(prompt: str, model_id: str | None = None) -> str:
    """Single-metric extraction via OpenRouter. Name kept for call-site compatibility."""
    try:
        return await llm.complete(prompt, model=model_id)
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Main streaming generator
# ─────────────────────────────────────────────────────────────────────────────

def _has_latin(text: str | None) -> bool:
    import re as _re
    return bool(_re.search(r"[A-Za-z]", text or ""))


def _has_cyrillic(text: str | None) -> bool:
    import re as _re
    return bool(_re.search(r"[А-Яа-яЁё]", text or ""))


def _looks_like_auditor_name(name: str | None) -> bool:
    normalized = (name or "").lower()
    return any(
        marker in normalized
        for marker in (
            "аудит",
            "аудитор",
            "audit",
            "tsatr",
            "цатр",
        )
    )


def _looks_like_generic_company_label(name: str | None) -> bool:
    import re as _re

    normalized = _re.sub(r"[^a-zа-яё0-9]+", " ", (name or "").lower()).strip()
    return normalized in {
        "bank",
        "банк",
        "report",
        "отчет",
        "отчёт",
        "financial statements",
        "финансовая отчетность",
        "финансовая отчётность",
    }


def _looks_like_filename_company_label(name: str | None, filename: str | None) -> bool:
    import re as _re

    if not name or not filename:
        return False
    cleaned_name = _re.sub(r"[^a-zа-яё0-9]+", " ", name.lower()).strip()
    stem = _re.sub(r"\.[a-z]{2,5}$", "", filename.lower(), flags=_re.I)
    cleaned_stem = _re.sub(r"[^a-zа-яё0-9]+", " ", stem).strip()
    if not cleaned_name or not cleaned_stem:
        return False
    return cleaned_name == cleaned_stem or cleaned_name in cleaned_stem or cleaned_stem in cleaned_name


def _company_name_from_filename(filename: str) -> str | None:
    """Pick the most likely brand name from a filename.

    Prefers all-uppercase Cyrillic acronyms (ЧТПЗ, ТМК, ВТБ, ОТП, СБЕР) — they
    are nearly always the issuer brand. Falls back to the longest capitalized
    Cyrillic word (e.g. "Сбербанк", "Кузнецкий"). Returns None if nothing
    looks like a brand.
    """
    import re as _re
    base = _re.sub(r"\.[a-z]{2,5}$", "", filename, flags=_re.I)
    tokens = _re.split(r"[_\-\s.]+", base)
    # 1) all-uppercase Cyrillic 3+ chars — acronyms
    acronyms = [t for t in tokens if _re.fullmatch(r"[А-ЯЁ]{3,8}", t or "")]
    if acronyms:
        # Longest — usually the most specific
        acronyms.sort(key=len, reverse=True)
        return acronyms[0]
    # 2) capitalized Cyrillic 4+ chars (Сбербанк, Лукойл) — exclude generic words
    _GENERIC = {"Отработано", "Отчёт", "Отчет", "Годовая", "Годовой",
                "Финансовая", "Бухгалтерский", "Консолидированная"}
    capitalized = [
        t for t in tokens
        if _re.fullmatch(r"[А-ЯЁ][а-яё]{3,15}", t or "") and t not in _GENERIC
    ]
    if capitalized:
        capitalized.sort(key=len, reverse=True)
        return capitalized[0]
    return None


def _reconcile_company_name(model_name: str, filename: str) -> str:
    """Sanity-check a model-extracted company name against the filename.

    Catches the subsidiary-vs-parent confusion: when a subsidiary's annual
    report says "входит в Группу ТМК" on every page, the LLM grabs "ТМК"
    even though the filename reads "ЧТПЗ 2024 годовая.pdf". This helper
    overrides the model's pick with the filename's acronym when the two
    are clearly disjoint inside the same script.

    English SEC issuer names must stay in their original spelling. A Cyrillic
    filename token such as "АМД" is only a user/file label and must not replace
    "ADVANCED MICRO DEVICES, INC." from the report cover.

    Logic:
      - Extract a brand-like token from the filename.
      - If the filename token is NOT a substring (case-insensitive) of the
        model name AND the model name is NOT a substring of the filename →
        the two names refer to different entities. Trust the filename.
      - Otherwise keep the model name (it's usually the more readable form).
    """
    if not model_name or not filename:
        return model_name or _company_name_from_filename(filename) or ""
    fname_brand = _company_name_from_filename(filename)
    if not fname_brand:
        return model_name
    if _has_latin(model_name) and _has_cyrillic(fname_brand) and not _has_cyrillic(model_name):
        return model_name
    a = model_name.lower()
    b = fname_brand.lower()
    base = filename.lower()
    if b in a or a in base:
        return model_name
    return fname_brand


def _prefer_detected_company_name(existing_name: str | None, detected_name: str | None, filename: str) -> str:
    """Choose a display company name without transliterating report names.

    `existing_name` can come from a user/project label or from older metadata.
    If the document detector finds an original Latin issuer name, prefer it over
    a Cyrillic short label such as "АМД"; then run the usual filename sanity
    check for same-script subsidiary/parent mistakes.
    """
    existing = (existing_name or "").strip()
    detected = (detected_name or "").strip()
    if not detected:
        return _reconcile_company_name(existing, filename)
    if not existing:
        return _reconcile_company_name(detected, filename)
    if (
        _looks_like_auditor_name(existing)
        or _looks_like_generic_company_label(existing)
        or _looks_like_filename_company_label(existing, filename)
    ):
        return _reconcile_company_name(detected, filename)
    if _has_latin(detected) and _has_cyrillic(existing) and not _has_cyrillic(detected):
        return _reconcile_company_name(detected, filename)
    return _reconcile_company_name(existing, filename)


def _detect_company_from_text(text: str) -> str | None:
    """Pull the issuer name out of the document head.

    Tries the long Russian legal-form forms first ("Публичного акционерного
    общества «X»"), then the short abbreviations (ПАО / АО / ОАО / ООО), then
    "Группа/Группе/Группой X" without quotes. Also handles SEC 10-K/10-Q covers
    where the issuer name appears immediately before "Exact name of registrant".
    Returns the inner name without the legal-form prefix.
    """
    import re as _re
    head = text[:20000]
    sec_patterns = [
        r"Commission File Number[^\n]{0,180}I\.R\.S\. Employer Identification No\.\s*\n\s*[0-9-]+\s+([A-Z0-9][A-Z0-9&.,'’\- ]{2,120}?)\s+\d{2}-\d{7}\b",
        r"\n\s*([A-Z][A-Z0-9&.,'’\- ]{3,120}?)\s*\n\s*\(Exact name of registrant as specified in its charter\)",
        r"\n\s*([A-Z][A-Z0-9&.,'’\- ]{3,120}?)\s*\n\s*Exact name of registrant",
    ]
    sec_stopwords = {
        "UNITED STATES",
        "SECURITIES AND EXCHANGE COMMISSION",
        "FORM 10-K",
        "FORM 10-Q",
        "WASHINGTON, D.C",
    }
    for pat in sec_patterns:
        m = _re.search(pat, head, flags=_re.I)
        if not m:
            continue
        name = " ".join(m.group(1).strip().split())
        if 3 <= len(name) <= 120 and name.upper() not in sec_stopwords:
            return name
    bank_stopwords = {
        "РОССИИ",
        "РОССИЙСКОЙ ФЕДЕРАЦИИ",
        "КЛИЕНТОВ",
        "БАНКОВ",
    }
    for raw_line in head.splitlines()[:120]:
        line = " ".join(raw_line.strip().split())
        if not line:
            continue
        bank_line = _re.fullmatch(
            r"(?:БАНК|Банк)\s+([А-ЯЁA-Z0-9][А-ЯЁA-Zа-яёa-z0-9 .\-]{1,60})",
            line,
        )
        if not bank_line:
            bank_line = _re.fullmatch(
                r"(?:ПАО|АО|ОАО)\s+(?:БАНК|Банк)\s+«?([^»\"]{2,60})»?",
                line,
            )
        if not bank_line:
            continue
        name = " ".join(bank_line.group(1).strip(" «»\"").split())
        if 2 <= len(name) <= 60 and name.upper() not in bank_stopwords:
            return name
    # Char class includes `.` so abbreviations with dots survive — e.g.
    # «ВЭБ.РФ», «ИК.РФ». Without it the Корпорация/Группа patterns
    # truncated state-corp names mid-token.
    patterns = [
        r"[Пп]убличн\w+\s+акционерн\w+\s+обществ\w+\s+«([^»]{2,80})»",
        r"[Аа]кционерн\w+\s+обществ\w+\s+«([^»]{2,80})»",
        r"[Гг]осударствен\w+\s+корпораци\w+(?:\s+\w+){0,2}\s+«([^»]{2,80})»",
        r"П\s*А\s*О\s+«НК\s+«([^»]{2,40})»",
        r"П\s*А\s*О\s+«([^»]{2,80})»",
        r"О\s*А\s*О\s+«([^»]{2,80})»",
        r"А\s*О\s+«([^»]{2,80})»",
        r"О\s*О\s*О\s+«([^»]{2,80})»",
        r"Групп\w+\s+«([^»]{2,80})»",
        r"Групп\w+\s+([А-ЯA-Z][А-ЯA-Zа-яa-z\-\.]{2,40})",
        r"Корпораци\w+\s+«([^»]{2,80})»",
        r"Корпораци\w+\s+([А-ЯA-Z][А-ЯA-Zа-яa-z\-\.]{2,40})",
        r"[Кк]омпани\w+\s+«([^»]{2,60})»",
    ]
    for pat in patterns:
        m = _re.search(pat, head, flags=_re.I)
        if m:
            name = " ".join(m.group(1).strip().split())
            name = _re.sub(r"^(ПАО|АО|ОАО|ООО)\s+", "", name, flags=_re.I)
            if 2 <= len(name) <= 80:
                return name
    return None


def _registry_phrases_for_segment(segment: str) -> list[str]:
    """Lower-cased phrases that, when found in PDF text, signal that the
    document belongs to the given segment (bank / non_financial).

    We use enums that exist in ONE registry but not the other — those are
    "segment-specific" markers (e.g. LOANS_STAGE3_GROSS exists only in
    EXTRACTABLE_BANK, INVENTORIES only in EXTRACTABLE_NON_FINANCIAL). Each
    metric contributes its Russian/English name plus up to 5 curated aliases.
    """
    nf_enums = {m["enum"] for m in EXTRACTABLE_NON_FINANCIAL}
    bank_enums = {m["enum"] for m in EXTRACTABLE_BANK}
    if segment == "bank":
        target_metrics = [m for m in EXTRACTABLE_BANK if m["enum"] not in nf_enums]
    else:
        target_metrics = [m for m in EXTRACTABLE_NON_FINANCIAL if m["enum"] not in bank_enums]
    phrases: list[str] = []
    for m in target_metrics:
        for key in (m.get("name_ru"), m.get("name_en")):
            if key and len(key) >= 6:
                phrases.append(key.strip().lower())
        for alias in (m.get("aliases_ru") or [])[:5]:
            if alias and alias != "Нет" and len(alias) >= 6:
                phrases.append(alias.strip().lower())
    # Dedupe and drop too-generic 1-word entries
    seen: set[str] = set()
    out: list[str] = []
    for p in phrases:
        if p in seen or " " not in p:
            continue
        seen.add(p)
        out.append(p)
    return out


_BANK_DETECTION_PHRASES = _registry_phrases_for_segment("bank")
_NF_DETECTION_PHRASES = _registry_phrases_for_segment("non_financial")


def _detect_company_type(text: str) -> str:
    """Pick 'bank' or 'non_financial' from registry-derived markers.

    Uses the segment-specific phrases assembled from the metric registry.
    Counts distinct phrase hits across the WHOLE document. Returns 'bank'
    only when bank markers clearly outnumber NF markers AND there are at
    least 3 of them — the registries share 35 metrics, single-word noise
    isn't enough to flip a non-financial PDF.
    """
    low = text.lower()
    bank_hits = sum(1 for p in _BANK_DETECTION_PHRASES if p in low)
    nf_hits = sum(1 for p in _NF_DETECTION_PHRASES if p in low)
    if bank_hits >= 3 and bank_hits > nf_hits:
        return "bank"
    return "non_financial"


async def _wait_for_pdf_text(
    txt_path: Path,
    timeout_s: int = 900,
    poll_s: float = 1.5,
) -> bool:
    """Poll for the .txt sidecar to appear, yielding to the event loop.

    The /upload endpoint kicks off `_parse_doc_in_background` as a
    BackgroundTask and returns immediately, so /extract can fire before
    parsing has finished writing the sidecar. Instead of erroring out
    right away (the old behavior — "Документ ещё обрабатывается, нажмите
    «Извлечь» снова"), wait up to `timeout_s` seconds for the file to
    show up. Most PDFs parse in 5-30s; long bank reports (100+ pages via
    IDP-OCR @ ~4s/page) need 7-12 min — hence the 15-min cap.
    """
    import asyncio
    import time as _t
    deadline = _t.time() + timeout_s
    while not txt_path.exists():
        if _t.time() > deadline:
            return False
        await asyncio.sleep(poll_s)
    return True


def _detect_year_from_text(text: str) -> str | None:
    """Most-mentioned recent year from the first 60K chars of the document."""
    import re as _re
    years_with_count: dict[int, int] = {}
    for pat in (
        r"31[\s.\-]+декабря[\s.\-]+(\d{4})",
        r"за\s+\d+\s*мес\.?\s*(\d{4})",
        r"12\s*мес\.?\s*(\d{4})",
        r"за\s+(\d{4})\s+год",
        r"за\s+год,?\s+закончивш\w+\s+31\s+декабря\s+(\d{4})",
    ):
        for m in _re.finditer(pat, text[:60000]):
            y = int(m.group(1))
            if 2010 <= y <= 2030:
                years_with_count[y] = years_with_count.get(y, 0) + 1
    if not years_with_count:
        return None
    return str(max(years_with_count.keys(), key=lambda y: (years_with_count[y], y)))


async def _extract_stream_unrestricted(
    doc_id: str,
    company_type: str,
    index=None,
    limit: Optional[int] = None,
    model_id: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """Finengine DEV — free-form extraction without the metric registry.

    The LLM is asked to surface ALL financial figures it can find in the
    report (revenue, EBITDA, segment splits, leverage ratios, even unusual
    items). Output shape mirrors registry-based extracts so the metrics
    table renders the same way; the only difference is `enum` is the
    Russian name (slug) instead of an English MetricEnum.
    """
    import json as _json
    import re as _re
    from pathlib import Path
    from finengine.config import settings
    from finengine.llm import openrouter as llm

    if index is None:
        from finengine.ingestion.indexer import VectorIndex
        index = VectorIndex()

    doc = index.get_document(doc_id)
    if not doc:
        yield {"type": "error", "message": f"Document {doc_id} not found"}
        return

    txt_path = Path(settings.docs_dir) / f"{doc_id}.txt"
    if not txt_path.exists():
        # PDF parse may still be running in the upload BackgroundTask. Wait
        # for the sidecar instead of erroring immediately — most reports
        # parse in 5-30s, hard timeout at 2 min as a safety net.
        if not await _wait_for_pdf_text(txt_path):
            yield {
                "type": "error",
                "message": (
                    "Документ ещё парсится (превышено окно ожидания 15 минут). "
                    "Большие отчёты (100+ страниц) могут парситься дольше — "
                    "подождите 1-2 минуты и нажмите «Извлечь» снова. "
                    "Повторно загружать документ не нужно."
                ),
            }
            return
    pdf_text = txt_path.read_text(encoding="utf-8")[:200_000]

    periods_detected, period_current, period_prev = _select_run_periods(pdf_text, doc.period)
    # Pick the best company name we can, then sanity-check against the
    # filename. The reconcile catches the subsidiary-vs-parent confusion
    # (e.g. ЧТПЗ 2024.pdf returning "ТМК" because the report repeats
    # "входит в Группу ТМК" on every page).
    detected_company = _detect_company_from_text(pdf_text)
    company_display = _prefer_detected_company_name(doc.company_name, detected_company, doc.filename)

    # Yield the start event before the LLM call so the UI can show progress.
    yield {
        "type": "start",
        "total_metrics": 0,         # unknown ahead of time
        "total_extractable": 0,
        "company_type": company_type,
        "company_name": company_display,
        "unit": "млн. руб.",
        "period_current": period_current,
        "period_prev": period_prev,
        "periods": periods_detected,
        "ocr_engine": getattr(doc, "ocr_engine", "") or "",
    }

    prompt = (
        f"# Финансовый отчёт компании {company_display or doc.filename}\n"
        f"Текущий период: {period_current}, предыдущий: {period_prev}.\n\n"
        "## Текст отчёта\n"
        f"{pdf_text}\n\n"
        "## Задача\n"
        "Извлеки ВСЕ финансовые показатели, которые ты видишь в отчёте. "
        "Не ограничивайся стандартным реестром — тяни любые цифры с финансовым "
        "смыслом: статьи баланса, P&L, cash flow, операционные KPI (объёмы "
        "производства, ARPU, MAU, etc.), сегментные разрезы, разовые "
        "корректировки, всё что встретится.\n\n"
        "## Правила\n"
        "1. Каждый показатель — со значением за текущий и предыдущий период.\n"
        "2. Числа без пробелов, как в отчёте. Скобки = отрицательное.\n"
        "3. Единица как в первоисточнике (млн руб., тыс. руб., %, шт., тонн, ...).\n"
        "4. Не выдумывай. Если значения нет — null.\n\n"
        "## Формат ответа (строго JSON, без markdown)\n"
        '{"metrics": [{"name": "Выручка", "current": 100200, "previous": 95300, "unit": "млн. руб."}, ...]}'
    )

    try:
        raw, _usage = await llm.complete_with_usage(
            prompt=prompt, system=None, model=model_id,
            response_format={"type": "json_object"}, max_output=6000,
        )
    except Exception as e:
        yield {"type": "error", "message": f"LLM error: {e}"}
        return

    # Parse JSON, tolerate ```json wrappers.
    s = (raw or "").strip()
    s = _re.sub(r"^```(?:json)?\s*", "", s)
    s = _re.sub(r"\s*```$", "", s)
    try:
        parsed = _json.loads(s)
    except Exception:
        m = _re.search(r"\{[\s\S]*\}", s)
        try:
            parsed = _json.loads(m.group(0)) if m else {}
        except Exception:
            parsed = {}

    metrics = parsed.get("metrics") if isinstance(parsed, dict) else None
    if not isinstance(metrics, list):
        yield {
            "type": "error",
            "code": "no_metrics_returned",
            "message": "Модель не вернула список показателей.",
        }
        return

    if limit is not None and limit > 0:
        metrics = metrics[:limit]

    def _coerce(v):
        if v is None or v == "" or (isinstance(v, str) and v.strip().lower() in ("null", "none", "—", "-")):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v).replace(" ", "").replace("\u00a0", "").replace(",", "."))
        except (TypeError, ValueError):
            return None

    seen_keys: set[str] = set()
    sent = 0
    for m in metrics:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or m.get("name_ru") or "").strip()
        if not name:
            continue
        # Slug as enum so the frontend has a stable React key.
        slug = _re.sub(r"\s+", "_", name.lower())[:80]
        slug = _re.sub(r"[^\wа-яё]+", "", slug, flags=_re.IGNORECASE)
        if not slug or slug in seen_keys:
            continue
        seen_keys.add(slug)
        v_cur = _coerce(m.get("current"))
        v_prv = _coerce(m.get("previous"))
        unit = (m.get("unit") or "").strip() or "млн. руб."
        # Same scale normalisation as the registry path.
        v_cur, norm_unit = _normalise_money_value(v_cur, unit)
        v_prv, _ = _normalise_money_value(v_prv, unit)
        period_values = [
            {"period": period_current, "value": v_cur, "unit": norm_unit or unit, "page": None, "source_text": ""},
            {"period": period_prev, "value": v_prv, "unit": norm_unit or unit, "page": None, "source_text": ""},
        ]
        yield {
            "type": "metric",
            "enum": slug.upper(),
            "name_ru": name,
            "value_current": v_cur,
            "value_prev": v_prv,
            "period_values": period_values,
            "unit": norm_unit or unit,
            "found": v_cur is not None,
            "is_calculated": False,
            "formula": "",
        }
        sent += 1

    yield {
        "type": "done",
        "period_current": period_current,
        "period_prev": period_prev,
        "periods": periods_detected,
        "unit": "млн. руб.",
        "company_type": company_type,
        "company_name": company_display,
        "_total_returned": sent,
    }


async def _extract_stream_full_context(
    doc_id: str,
    company_type: str,
    index=None,
    limit: Optional[int] = None,
    model_id: Optional[str] = None,
    include_segments: bool = False,
    include_consolidated_group: bool = False,
    metric_enums: Optional[set[str]] = None,
    full_registry: bool = False,
    role: str = "",
    segment_mode: str = "extended",
    segment_context_mode: str = "full",
    debug_dir: Optional[Path] = None,
    system_prompt_override: Optional[str] = None,
    user_prompt_builder: Optional[Callable[..., str]] = None,
    segment_metrics_override: Optional[set[str]] = None,
    segment_rules_extra: Optional[str] = None,
    default_unit_override: Optional[str] = None,
    fallback_period_label_override: Optional[str] = None,
    registry_override: Optional[dict] = None,
    include_top_borrowers: bool = False,
    period_calendar: str = "calendar",
    disable_segments: bool = False,
    reporting_standard_override: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """Full-context extraction: send whole PDF text + metric registry → JSON.

    Replaces the per-metric FAISS RAG loop. Single LLM call returns a JSON
    object {METRIC_ENUM: {values: {period: raw_cell}, unit_text, page}} which
    is fanned out to the same per-metric event shape as the legacy stream plus
    `period_values` for multi-period consumers.

    Scope warning: the generic full-context entrypoint keeps the legacy
    AIFA-trained 55-metric subset when neither ``metric_enums`` nor
    ``full_registry`` is supplied. Finengine DEV callers must use
    ``extract_stream_finengine_dev`` so production and quality tests cannot
    silently drift back to that compatibility scope.
    """
    import json as _json
    from pathlib import Path
    from finengine.config import settings

    if index is None:
        from finengine.ingestion.indexer import VectorIndex
        index = VectorIndex()

    doc = index.get_document(doc_id)
    if not doc:
        yield {"type": "error", "message": f"Document {doc_id} not found"}
        return
    processing_mode = processing_mode_for_document(doc).value
    if not supports_metric_extraction(processing_mode):
        yield {
            "type": "error",
            "message": (
                "Классификатор не обнаружил в документе полный или сокращенный "
                "набор отчетных показателей. Для этого документа используйте «Инсайты»."
            ),
        }
        return
    registry_document_type = canonical_document_type_for_processing_mode(
        processing_mode
    )
    # Results releases and presentations share the press-release registry
    # bucket, but prompts and public events must retain the real source kind.
    raw_document_type = str(
        getattr(
            getattr(doc, "doc_type", ""),
            "value",
            getattr(doc, "doc_type", ""),
        )
        or ""
    ).strip().lower()
    source_document_type = (
        raw_document_type
        if (
            registry_document_type == "press_release"
            and raw_document_type in _NON_REPORT_SOURCE_LABELS_EN
        )
        else registry_document_type
    )

    # Load full text from .txt sidecar (written by the background task after
    # upload). Wait for it instead of erroring out right away — typical
    # parse is 5-30s, hard timeout 2 min as a safety net.
    txt_path = Path(settings.docs_dir) / f"{doc_id}.txt"
    raw_idp_path = Path(settings.docs_dir) / f"{doc_id}.idpocr.v2.json"
    if not txt_path.exists():
        yield {
            "type": "status",
            "stage": "document_preparation",
            "message": "Подготавливаем документ к анализу",
        }
        if not await _wait_for_pdf_text(txt_path):
            yield {
                "type": "error",
                "message": (
                    "Документ ещё парсится (превышено окно ожидания 15 минут). "
                    "Большие отчёты (100+ страниц) могут парситься дольше — "
                    "подождите 1-2 минуты и нажмите «Извлечь» снова. "
                    "Повторно загружать документ не нужно."
                ),
            }
            return
    pdf_text = txt_path.read_text(encoding="utf-8")
    if "===PAGE " not in pdf_text:
        if raw_idp_path.exists():
            try:
                from finengine.skills.aifa_baseline.idp_ocr_client import (
                    markdown_from_saved_idp_response,
                )

                saved_idp = _json.loads(raw_idp_path.read_text(encoding="utf-8"))
                paginated_idp_text = markdown_from_saved_idp_response(saved_idp)
                if paginated_idp_text.strip():
                    pdf_text = paginated_idp_text
            except Exception:
                # The flat sidecar remains a valid fallback for older uploads.
                pass

    # Try to rebuild a *paginated* version of the doc from FAISS chunks so
    # the LLM can cite a page per metric (`Источник` column on the UI).
    # Fallback to the flat sidecar text when no chunk metadata is available
    # — the prompt then drops the page-citation instructions and `page`
    # comes back as null.
    by_page: dict[int, list[str]] = {}
    for kind in ("text", "financial_table", "table"):
        try:
            chunks_p = index.get_chunks_by_type(kind, doc_id=doc_id)
        except Exception:
            chunks_p = []
        for c in chunks_p:
            p = c.metadata.get("page", 0) if hasattr(c, "metadata") else 0
            try:
                p = int(p or 0)
            except Exception:
                p = 0
            if p > 0:
                by_page.setdefault(p, []).append(c.content or "")
    paginated_text = ""
    if by_page:
        parts = []
        for p in sorted(by_page.keys()):
            parts.append(f"===PAGE {p}===\n" + "\n\n".join(by_page[p]))
        paginated_text = "\n\n".join(parts)
    has_sidecar_pages = "===PAGE " in pdf_text
    has_pages = bool(paginated_text) or has_sidecar_pages
    pdf_text_for_prompt = paginated_text or pdf_text

    effective_type = company_type
    if company_type in ("unknown", "auto", ""):
        effective_type = _detect_company_type(pdf_text)

    if registry_override is not None:
        # Per-cell registry: the calling cell resolved its own base + overrides
        # (hints/aliases/add/drop) via registry_for_profile and passes the rows
        # in. Empty overrides → identical rows to the company_type branch below,
        # so this stays a no-op until a cell actually ships an override file.
        extractable = registry_override.get("extractable") or []
        calculated_defs = registry_override.get("calculated") or []
    elif effective_type == "bank":
        extractable = EXTRACTABLE_BANK
        calculated_defs = CALCULATED_BANK
    else:
        if metric_enums or full_registry:
            extractable = EXTRACTABLE_NON_FINANCIAL
        else:
            # Align Finengine Baseline with AIFA Baseline: same 55-metric NF scope
            # (the subset Расул's reranker was trained on, what every AIFA run
            # extracts). For explicit GT-scoped runs, keep the full registry so
            # every enum present in GT can actually be prompted.
            from finengine.skills.aifa_baseline.extractor import filter_to_trained
            extractable = filter_to_trained(EXTRACTABLE_NON_FINANCIAL)
        calculated_defs = CALCULATED_NON_FINANCIAL

    if metric_enums:
        wanted = {str(item).strip().upper() for item in metric_enums if str(item).strip()}
        if wanted:
            extractable = [m for m in extractable if str(m.get("enum", "")).strip().upper() in wanted]
            calculated_defs = [m for m in calculated_defs if str(m[0]).strip().upper() in wanted]

    registry_extractable_count = len(extractable)
    registry_calculated_count = len(calculated_defs)
    extractable = extractable_for_processing_mode(
        extractable,
        processing_mode,
    )

    from finengine.skills.top_borrowers import TOP_BORROWERS_ENUM

    top_borrowers_metric = next(
        (
            m for m in extractable
            if str(m.get("enum", "")).strip().upper() == TOP_BORROWERS_ENUM
        ),
        None,
    )
    # TOP_BORROWERS is a hidden structured input for formulas. It is extracted
    # by a dedicated mini-stage and must not be prompted/emitted as a regular
    # scalar metric.
    extractable = [
        m for m in extractable
        if str(m.get("enum", "")).strip().upper() != TOP_BORROWERS_ENUM
    ]

    segment_extractable = list(extractable)

    if limit is not None and limit > 0:
        extractable = extractable[:limit]
        calculated_defs = []

    extracted_enum_set = {str(m.get("enum", "")).strip().upper() for m in extractable}
    if source_document_type == "financial_report":
        calculated_defs = [
            m for m in calculated_defs
            if str(m[0]).strip().upper() not in extracted_enum_set
        ]
    else:
        calculated_defs = source_relevant_calculated_defs(
            calculated_defs,
            extractable,
        )

    total_metrics = len(extractable) + len(calculated_defs)

    periods_detected, period_current, period_prev = _select_run_periods(
        pdf_text,
        doc.period,
        fallback_period_label=fallback_period_label_override or "Текущий",
        period_calendar=period_calendar,
    )
    fiscal_context = FiscalPeriodContext.from_text(pdf_text)
    max_report_year = _max_report_anchor_year_for_calendar(periods_detected, period_calendar)
    reporting_standard = reporting_standard_override or _detect_reporting_standard(pdf_text_for_prompt)
    default_unit = default_unit_override or _default_unit_for_standard(reporting_standard)
    source_form_by_page = _build_source_form_by_page(
        pdf_text_for_prompt,
        reporting_standard=reporting_standard,
        role=role,
    )

    # Auto-detect company name on every extract — covers documents uploaded
    # before the upload-time detector existed and the user-empty case.
    # Pick the best company name we can, then sanity-check against the
    # filename. The reconcile catches the subsidiary-vs-parent confusion
    # (e.g. ЧТПЗ 2024.pdf returning "ТМК" because the report repeats
    # "входит в Группу ТМК" on every page).
    detected_company = _detect_company_from_text(pdf_text)
    company_display = _prefer_detected_company_name(doc.company_name, detected_company, doc.filename)

    yield {
        "type": "start",
        "total_metrics": total_metrics,
        "total_extractable": len(extractable),
        "company_type": effective_type,
        "company_name": company_display,
        "doc_type": source_document_type,
        "processing_mode": processing_mode,
        "unit": default_unit,
        "period_current": period_current,
        "period_prev": period_prev,
        "periods": periods_detected,
        "period_calendar": period_calendar,
    }

    yield {
        "type": "status",
        "stage": "report_analysis",
        "message": (
            "Анализируем отчет и сопоставляем показатели"
            if source_document_type == "financial_report"
            else "Анализируем документ и сопоставляем показатели"
        ),
    }

    # Build the rich extraction prompt. Two sections:
    #   1) Информация о показателях — for each metric: name_ru/en, synonyms,
    #      description (что это), hint (где в отчёте искать).
    #   2) Схема ответа — JSON shape per ENUM.
    # The model gets concrete language variants AND search instructions, which
    # raises both recall (finds metric under non-canonical wording) and
    # precision (less likely to grab a wrong line that just happens to share a
    # word with the metric name).
    info_blocks: list[str] = []
    schema_lines: list[str] = []
    for m in extractable:
        # Info block
        block = [f"### {m['enum']}: {m['name_ru']}"]
        if m.get("name_en"):
            block.append(f"  (англ.: {m['name_en']})")
        aliases = []
        for a in (m.get("aliases_ru") or []) + (m.get("aliases_en") or []):
            if a and a != "Нет":
                aliases.append(a)
        if aliases:
            block.append(f"  Также может называться: {'; '.join(aliases[:6])}")
        if m.get("description"):
            block.append(f"  Описание: {m['description']}")
        if m.get("hint"):
            block.append(f"  Где искать: {m['hint']}")
        # E-Reg-4: explicit gross/net contract. Renders only for metrics
        # the registry annotates — keeps prompt noise minimal.
        vb = m.get("value_basis")
        if vb == "gross":
            block.append("  ⚠️ БАЗИС: ВАЛОВОЕ значение (gross — ДО вычета резерва/амортизации). НЕ нетто.")
        elif vb == "net":
            block.append("  ⚠️ БАЗИС: НЕТТО значение (net — ПОСЛЕ вычета резерва/амортизации).")
        info_blocks.append("\n".join(block))

        # Schema line. Verbatim transcript schema (E-1 fix, 2026-05-05):
        # model returns the cell text AS-WRITTEN by period header.
        # Deterministic Python parser converts text→number, so the LLM
        # can never multiply by 1000 by accident (the Sber failure mode), and
        # deterministic period normalization handles shifted fiscal calendars.
        # `page` is the 1-based page number — present only when the prompt
        # is paginated with `===PAGE N===` markers.
        if reporting_standard.endswith("_en"):
            period_placeholder = "<period exactly as shown in the table/header>"
            value_placeholder = "<cell value exactly as written or null>"
            unit_placeholder = "<unit exactly as shown in the table/column header>"
            page_placeholder = "<page number or null>"
        else:
            period_placeholder = "<период как в шапке таблицы>"
            value_placeholder = "<буквально как в ячейке или null>"
            unit_placeholder = "<буквально из шапки колонки/таблицы>"
            page_placeholder = "<номер страницы или null>"

        if has_pages:
            schema = (
                f'{{"values": {{"{period_placeholder}": '
                f'"{value_placeholder}"}}, '
                f'"unit_text": "{unit_placeholder}", '
                f'"page": {page_placeholder}}}'
            )
            schema_lines.append(
                f'"{m["enum"]}": {schema}'
            )
        else:
            schema = (
                f'{{"values": {{"{period_placeholder}": '
                f'"{value_placeholder}"}}, '
                f'"unit_text": "{unit_placeholder}"}}'
            )
            schema_lines.append(
                f'"{m["enum"]}": {schema}'
            )

    # Cell ownership hook: if a cell handed us its own system_prompt,
    # use it verbatim. Otherwise call into the shared legacy assembly —
    # same source of truth cells use, so RU-prompt strings live in one
    # place (_shared/legacy_assemblies.py) rather than being duplicated
    # here and there. Lazy import keeps bulk_extractor → extractors
    # acyclic.
    if system_prompt_override is not None:
        system_prompt = system_prompt_override
    else:
        from finengine.skills.extractors._shared.legacy_assemblies import (
            build_legacy_ru_system_prompt,
        )
        system_prompt = build_legacy_ru_system_prompt(reporting_standard)
    source_specific_rules = (
        _source_specific_actual_rules_en(source_document_type)
        if reporting_standard.endswith("_en")
        else _source_specific_actual_rules_ru(source_document_type)
    )
    if source_specific_rules:
        system_prompt = system_prompt + "\n\n" + source_specific_rules

    if has_pages and reporting_standard.endswith("_en"):
        page_instructions = (
            "\n\n# Page citations\n"
            "The report text is split into pages with markers like `===PAGE N===`. "
            "For every metric you find, return the page number N where the value "
            "was taken in the `page` field. If a value is derived from multiple "
            "pages, use the page with the final reported total. If the value is "
            "not found, return `page: null`."
        )
    elif has_pages:
        page_instructions = (
            "\n\n# Цитирование страниц\n"
            "Текст отчёта разбит на страницы маркерами вида `===PAGE N===`. Для "
            "каждой найденной метрики верни номер страницы N, на которой ты "
            "взял значение, в поле `page`. Если значение получено из нескольких "
            "страниц — укажи ту, где найден итог. Если значение не найдено — "
            "`page: null`."
        )
    else:
        page_instructions = ""

    # Same pattern as system_prompt: cell-provided builder → use it;
    # otherwise call the shared RU assembly so the prompt strings stay
    # in one place.
    if user_prompt_builder is not None:
        builder = user_prompt_builder
    else:
        from finengine.skills.extractors._shared.legacy_assemblies import (
            build_legacy_ru_user_prompt,
        )
        builder = build_legacy_ru_user_prompt
    user_prompt = builder(
        pdf_text_for_prompt=pdf_text_for_prompt,
        period_current=period_current,
        period_prev=period_prev,
        periods_detected=periods_detected,
        info_blocks=info_blocks,
        page_instructions=page_instructions,
        schema_lines=schema_lines,
        include_non_financial_industry_note=effective_type != "bank",
    )
    _write_llm_debug_file(debug_dir, "main_system.md", system_prompt)
    _write_llm_debug_file(debug_dir, "main_prompt.md", user_prompt)

    def _coerce_num(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            s = v.strip().replace(" ", "").replace("\u00a0", "")
            # Russian decimal comma handling. If string has exactly one
            # comma followed by 1-2 digits → decimal: "951,5" → 951.5;
            # "12,34" → 12.34. If 3 digits after comma (or multiple commas)
            # → thousand grouping like "1,234,567" or "12,345" → strip.
            comma_count = s.count(",")
            if comma_count == 1:
                head, tail = s.split(",", 1)
                # 1-2 digits after the comma + plain digits before → decimal
                if 1 <= len(tail) <= 2 and tail.isdigit() and head.replace("-", "").replace(".", "").isdigit():
                    s = head + "." + tail
                else:
                    # Fall back: strip the comma (treat as thousand separator)
                    s = s.replace(",", "")
            elif comma_count > 1:
                # US-style thousands grouping like "1,234,567"
                s = s.replace(",", "")
            try:
                return float(s)
            except Exception:
                return None
        return None

    extracted_current: dict[str, Optional[float]] = {}
    extracted_prev: dict[str, Optional[float]] = {}
    extracted_by_period: dict[str, dict[str, Optional[float]]] = {}
    extractable_period_values_by_enum: dict[str, list[dict]] = {}
    metric_by_enum = {m["enum"]: m for m in extractable}
    valid_enums = set(metric_by_enum.keys())
    formula_default_abs_enums = {
        str(m.get("enum", "")).strip().upper()
        for m in extractable
        if m.get("formula_default_abs_behavior")
    }

    # Stage timings + first-tokens latency for the run logger.
    import time as _ttiming
    diag: dict = {
        "pdf_text_chars": len(pdf_text),
        "schema_lines": len(schema_lines),
        "doc_type": source_document_type,
        "processing_mode": processing_mode,
        "registry_extractable_before_source_scope": registry_extractable_count,
        "registry_calculated_before_source_scope": registry_calculated_count,
        "source_scoped_extractable": len(extractable),
        "source_scoped_calculated": len(calculated_defs),
        "llm_stream_s": 0.0,
        "first_token_s": None,
        "calc_s": 0.0,
        "include_consolidated_group": bool(include_consolidated_group),
        "source_forms_enabled": bool(source_form_by_page),
    }
    if source_form_by_page:
        diag["source_form_pages"] = {
            label: sum(1 for value in source_form_by_page.values() if value == label)
            for label in (
                SOURCE_FORM_BALANCE,
                SOURCE_FORM_PNL,
                SOURCE_FORM_CASH_FLOW,
                SOURCE_FORM_EQUITY,
                SOURCE_FORM_NOTES,
            )
        }
    usage_sink: dict = {"in": 0, "out": 0, "cached": 0}
    llm_t0 = _ttiming.time()
    consolidated_group_task: asyncio.Task[list[dict]] | None = None
    segment_task: asyncio.Task[list[dict]] | None = None

    async def _collect_consolidated_group_events() -> list[dict]:
        events: list[dict] = []
        async for group_event in _extract_consolidated_group_full_context(
            pdf_text_for_prompt=pdf_text_for_prompt,
            period_current=period_current,
            period_prev=period_prev,
            company_display=company_display or doc.company_name or doc.filename,
            reporting_standard=reporting_standard,
            company_type=effective_type,
            model_id=model_id,
            debug_dir=debug_dir,
        ):
            events.append(group_event)
        return events

    async def _collect_segment_events() -> list[dict]:
        events: list[dict] = []
        async for segment_event in _extract_segment_breakdown_full_context(
            pdf_text_for_prompt=pdf_text_for_prompt,
            extractable=segment_extractable,
            period_current=period_current,
            period_prev=period_prev,
            company_display=company_display or doc.company_name or doc.filename,
            effective_type=effective_type,
            model_id=model_id,
            segment_mode=segment_mode,
            segment_context_mode=segment_context_mode,
            debug_dir=debug_dir,
            segment_metrics_override=segment_metrics_override,
            segment_rules_extra=segment_rules_extra,
            period_calendar=period_calendar,
            document_type=source_document_type,
            reporting_standard_override=reporting_standard,
            default_unit_override=default_unit,
        ):
            events.append(segment_event)
        return events

    if include_consolidated_group:
        consolidated_group_task = asyncio.create_task(_collect_consolidated_group_events())
    if not disable_segments and (include_segments or _role_wants_segments(role)):
        segment_task = asyncio.create_task(_collect_segment_events())

    # Stream the response and emit metric events as soon as each enum block
    # parses successfully — gives the UI live progress instead of a 30s wait.
    buffer = ""
    emitted: set[str] = set()
    try:
        async for delta in llm.stream(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model_id,
            response_format={"type": "json_object"},
            max_output=32000,
            usage_sink=usage_sink,
        ):
            if diag["first_token_s"] is None and delta:
                diag["first_token_s"] = round(_ttiming.time() - llm_t0, 2)
            buffer += delta
            for enum, block in _metric_block_iter(buffer, valid_enums):
                if enum in emitted or enum not in valid_enums:
                    continue
                try:
                    info = _json.loads(block)
                except Exception:
                    continue
                emitted.add(enum)
                metric = metric_by_enum[enum]
                # Verbatim path (E-1 fix): prefer new `values` map by period,
                # fall back to legacy current/previous fields for older mocks.
                period_values, metric_unit = _coerce_period_values(
                    info,
                    enum=enum,
                    period_current=period_current,
                    period_prev=period_prev,
                    default_unit=default_unit,
                    abs_cost=False,
                    max_report_year=max_report_year,
                    fiscal_context=fiscal_context,
                    period_calendar=period_calendar,
                )
                for pv in period_values:
                    if pv.get("value") is not None:
                        extracted_by_period.setdefault(pv["period"], {})[enum] = pv.get("value")
                extractable_period_values_by_enum[enum] = period_values
                v_cur = _value_for_period(period_values, period_current, period_calendar)
                if v_cur is None:
                    v_cur = _first_value(period_values)
                v_prv = _value_for_period(period_values, period_prev, period_calendar)
                extracted_current[enum] = v_cur
                extracted_prev[enum] = v_prv
                yield _apply_metric_source_forms({
                    "type": "metric",
                    "enum": enum,
                    "name_ru": metric["name_ru"],
                    "value_current": v_cur,
                    "value_prev": v_prv,
                    "period_values": period_values,
                    "unit": (period_values[0].get("unit") if period_values else None) or metric_unit,
                    "found": any(p.get("value") is not None for p in period_values),
                    "is_calculated": False,
                    "formula": "",
                    "page": _page_from_period_values(period_values),
                }, source_form_by_page)
    except Exception as e:
        for task in (consolidated_group_task, segment_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        from finengine.skills.aifa_baseline.extractor import _user_facing_llm_error
        yield {"type": "error", "message": _user_facing_llm_error(str(e))}
        return

    diag["llm_stream_s"] = round(_ttiming.time() - llm_t0, 1)
    diag["raw_chars"] = len(buffer)
    diag["streamed_emitted"] = len(emitted)
    _write_llm_debug_file(debug_dir, "main_response.json", buffer)
    if not buffer.strip():
        for task in (consolidated_group_task, segment_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        yield {
            "type": "error",
            "message": (
                "Модель не вернула ответ по извлечению. "
                "Пожалуйста, запустите извлечение ещё раз."
            ),
        }
        return

    yield {
        "type": "status",
        "stage": "table_build",
        "message": "Формируем таблицу показателей",
    }

    # If the streaming pass missed any enums (model format drift, partial
    # output), do one final pass over the full buffer with our robust parser.
    leftover_parsed = _parse_extract_json(buffer) or {}
    for metric in extractable:
        enum = metric["enum"]
        if enum in emitted:
            continue
        info = leftover_parsed.get(enum) or {}
        # Verbatim path (E-1 fix), same as streaming branch.
        period_values, metric_unit = _coerce_period_values(
            info,
            enum=enum,
            period_current=period_current,
            period_prev=period_prev,
            default_unit=default_unit,
            abs_cost=False,
            max_report_year=max_report_year,
            fiscal_context=fiscal_context,
            period_calendar=period_calendar,
        )
        for pv in period_values:
            if pv.get("value") is not None:
                extracted_by_period.setdefault(pv["period"], {})[enum] = pv.get("value")
        extractable_period_values_by_enum[enum] = period_values
        v_cur = _value_for_period(period_values, period_current, period_calendar)
        if v_cur is None:
            v_cur = _first_value(period_values)
        v_prv = _value_for_period(period_values, period_prev, period_calendar)
        extracted_current[enum] = v_cur
        extracted_prev[enum] = v_prv
        yield _apply_metric_source_forms({
            "type": "metric",
            "enum": enum,
            "name_ru": metric["name_ru"],
            "value_current": v_cur,
            "value_prev": v_prv,
            "period_values": period_values,
            "unit": (period_values[0].get("unit") if period_values else None) or metric_unit,
            "found": any(p.get("value") is not None for p in period_values),
            "is_calculated": False,
            "formula": "",
            "page": _page_from_period_values(period_values),
        }, source_form_by_page)

    if include_top_borrowers and top_borrowers_metric:
        from finengine.skills import top_borrowers as _top_borrowers

        try:
            top_n = max(1, int(getattr(settings, "top_borrowers_count", 5) or 5))
        except Exception:
            top_n = 5
        try:
            top_period_values, top_diag = await _top_borrowers.extract_top_borrowers_period_values(
                pdf_text_for_prompt,
                ocr_json_path=raw_idp_path if raw_idp_path.exists() else None,
                top_n=top_n,
                periods_detected=periods_detected,
                period_current=period_current,
                period_prev=period_prev,
                default_unit=default_unit,
                model_id=model_id,
                debug_dir=debug_dir,
                stream_fn=llm.stream,
            )
            diag.update(top_diag)
        except Exception as exc:
            top_period_values = []
            diag["top_borrowers_error"] = str(exc)[:500]

        if top_period_values:
            extractable_period_values_by_enum[TOP_BORROWERS_ENUM] = top_period_values
            for pv in top_period_values:
                if pv.get("value") is not None:
                    extracted_by_period.setdefault(pv["period"], {})[TOP_BORROWERS_ENUM] = pv.get("value")
            top_cur = _value_for_period(top_period_values, period_current, period_calendar)
            if top_cur is None:
                top_cur = _first_value(top_period_values)
            top_prv = _value_for_period(top_period_values, period_prev, period_calendar)
            extracted_current[TOP_BORROWERS_ENUM] = top_cur
            extracted_prev[TOP_BORROWERS_ENUM] = top_prv
            yield {
                "type": "hidden_metric",
                "enum": TOP_BORROWERS_ENUM,
                "name_ru": (top_borrowers_metric or {}).get("name_ru") or "Топ заемщиков по секторам экономики",
                "value_current": top_cur,
                "value_prev": top_prv,
                "period_values": top_period_values,
                "unit": (top_period_values[0].get("unit") if top_period_values else None) or default_unit,
                "found": True,
                "is_calculated": False,
                "formula": "",
                "page": _page_from_period_values(top_period_values),
            }
        elif top_borrowers_metric:
            extracted_current.setdefault(TOP_BORROWERS_ENUM, None)
            extracted_prev.setdefault(TOP_BORROWERS_ENUM, None)

    # Calculated metrics — same logic as the RAG path.
    calc_t0 = _ttiming.time()
    calc_current, calc_prev = _calculate_metrics_pair(
        calculated_defs,
        extracted_current,
        extracted_prev,
        formula_default_abs_enums=formula_default_abs_enums,
    )
    diag["calc_s"] = round(_ttiming.time() - calc_t0, 2)
    from finengine.skills.pricing import aggregate_usage_to_diag, cost_usd
    usage_sink["calls"] = 1
    aggregate_usage_to_diag(usage_sink, diag, model_id)
    usage_model = str(usage_sink.get("model") or model_id or "")
    llm_requests = [
        {
            "stage": "main",
            "model": usage_model,
            "elapsed_s": diag["llm_stream_s"],
            "tokens_in": diag["tokens_in"],
            "tokens_out": diag["tokens_out"],
            "tokens_cached": diag["tokens_cached"],
            "cost_usd": round(
                cost_usd(
                    usage_model,
                    diag["tokens_in"],
                    diag["tokens_out"],
                    diag["tokens_cached"],
                ),
                4,
            ),
        }
    ]
    if "top_borrowers_llm_s" in diag:
        top_usage_model = str(model_id or usage_model or "")
        top_tokens_in = int(diag.get("top_borrowers_tokens_in") or 0)
        top_tokens_out = int(diag.get("top_borrowers_tokens_out") or 0)
        top_tokens_cached = int(diag.get("top_borrowers_tokens_cached") or 0)
        llm_requests.append(
            {
                "stage": "top_borrowers",
                "model": top_usage_model,
                "elapsed_s": diag["top_borrowers_llm_s"],
                "tokens_in": top_tokens_in,
                "tokens_out": top_tokens_out,
                "tokens_cached": top_tokens_cached,
                "cost_usd": round(
                    cost_usd(
                        top_usage_model,
                        top_tokens_in,
                        top_tokens_out,
                        top_tokens_cached,
                    ),
                    4,
                ),
            }
        )
    diag["llm_requests"] = llm_requests
    all_periods = _filter_plausible_report_periods(
        sort_periods(list(extracted_by_period.keys())) or periods_detected,
        max_report_year=max_report_year,
    )
    calculation_extracted_by_period = _with_point_values_for_duration_calculations(
        extracted_by_period,
        all_periods,
        period_calendar=period_calendar,
    )
    calc_by_period = _calculate_metrics_for_periods(
        calculated_defs,
        calculation_extracted_by_period,
        all_periods,
        formula_default_abs_enums=formula_default_abs_enums,
        period_calendar=period_calendar,
    )
    display_calc_by_period = _calculate_metrics_for_periods(
        calculated_defs,
        extracted_by_period,
        all_periods,
        formula_default_abs_enums=formula_default_abs_enums,
        period_calendar=period_calendar,
    )
    calc_period_kinds = _calculated_period_kind_map(calculated_defs)
    for enum, name_ru, formula, _deps in calculated_defs:
        is_pct = "* 100" in formula or "× 100" in formula
        unit_calc = "%" if is_pct else default_unit
        source_calc_by_period = (
            display_calc_by_period
            if calc_period_kinds.get(str(enum).strip().upper()) == POINT_KIND
            else calc_by_period
        )
        period_values = [
            {
                "period": p,
                "value": source_calc_by_period.get(p, {}).get(enum),
                "unit": unit_calc,
                "page": None,
                "source_text": "",
            }
            for p in all_periods
            if source_calc_by_period.get(p, {}).get(enum) is not None
        ]
        v_cur = calc_current.get(enum)
        if v_cur is None:
            v_cur = _value_for_period(period_values, period_current, period_calendar)
        v_prv = calc_prev.get(enum)
        if v_prv is None:
            v_prv = _value_for_period(period_values, period_prev, period_calendar)
        yield _apply_metric_source_forms({
            "type": "metric",
            "enum": enum,
            "name_ru": name_ru,
            "value_current": v_cur,
            "value_prev": v_prv,
            "period_values": period_values,
            "unit": unit_calc,
            "found": v_cur is not None,
            "is_calculated": True,
            "formula": formula,
        }, source_form_by_page)

    if segment_task is not None:
        segment_events: list[dict] = []
        try:
            segment_raw_events = await segment_task
        except Exception as exc:
            segment_raw_events = []
            diag["segment_error"] = f"{type(exc).__name__}: {exc}"[:300]
        for segment_event in segment_raw_events:
            if segment_event.get("type") == "segment_diag":
                segment_diag = segment_event.get("_diag_segment") or {}
                existing_requests = list(diag.get("llm_requests") or [])
                diag.update(segment_diag)
                diag["llm_requests"] = [
                    *existing_requests,
                    *(segment_diag.get("llm_requests") or []),
                ]
                continue
            if segment_event.get("type") == "error":
                diag["segment_error"] = segment_event.get("message", "")
                continue
            segment_events.append(segment_event)

        segment_total_enums = {
            event.get("enum")
            for event in segment_events
            if event.get("type") == "metric" and str(event.get("segment") or "").upper() == "TOTAL"
        }
        if segment_metrics_override is not None:
            scoped = [m for m in segment_extractable if m.get("enum") in segment_metrics_override]
        else:
            scoped = _segment_metric_scope(segment_extractable, reporting_standard)
        segment_metric_by_enum = {metric["enum"]: metric for metric in scoped}
        total_fallback_events: list[dict] = []
        for enum, metric in segment_metric_by_enum.items():
            if enum in segment_total_enums:
                continue
            period_values = [
                dict(pv)
                for pv in extractable_period_values_by_enum.get(enum, [])
                if pv.get("value") is not None
            ]
            if not period_values:
                continue
            total_fallback_events.append(
                _main_total_fallback_event(
                    metric=metric,
                    period_values=period_values,
                    period_current=period_current,
                    period_prev=period_prev,
                    unit=default_unit,
                )
            )

        if total_fallback_events:
            diag["segment_total_from_main_fallback"] = [
                event.get("enum") for event in total_fallback_events
            ]
        for segment_event in [*segment_events, *total_fallback_events]:
            yield _apply_metric_source_forms(segment_event, source_form_by_page)

    if consolidated_group_task is not None:
        try:
            consolidated_group_events = await consolidated_group_task
        except Exception as exc:
            consolidated_group_events = []
            diag["consolidated_group_error"] = f"{type(exc).__name__}: {exc}"[:300]
        for group_event in consolidated_group_events:
            if group_event.get("type") == "consolidated_group_diag":
                group_diag = group_event.get("_diag_consolidated_group") or {}
                existing_requests = list(diag.get("llm_requests") or [])
                diag.update(group_diag)
                diag["llm_requests"] = [
                    *existing_requests,
                    *(group_diag.get("llm_requests") or []),
                ]
                continue
            if group_event.get("type") == "error":
                diag["consolidated_group_error"] = group_event.get("message", "")
                continue
            yield _apply_metric_source_forms(group_event, source_form_by_page)

    display_period_current, display_period_prev = _select_display_periods(
        all_periods or periods_detected,
        period_current,
        period_prev,
        period_calendar=period_calendar,
    )

    yield {
        "type": "done",
        "period_current": display_period_current,
        "period_prev": display_period_prev,
        "periods": all_periods or periods_detected,
        "period_calendar": period_calendar,
        "unit": default_unit,
        "company_type": effective_type,
        "company_name": company_display or doc.company_name,
        "doc_type": source_document_type,
        "processing_mode": processing_mode,
        "ocr_engine": getattr(doc, "ocr_engine", "") or "",
        "_diag": diag,
    }


async def extract_stream_finengine_dev(
    doc_id: str,
    company_type: str,
    index=None,
    limit: Optional[int] = None,
    model_id: Optional[str] = None,
    include_segments: bool = True,
    include_consolidated_group: bool = False,
    metric_enums: Optional[set[str]] = None,
    role: str = "",
    segment_mode: str = "extended",
    segment_context_mode: str = "full",
    debug_dir: Optional[Path] = None,
    include_top_borrowers: bool = False,
) -> AsyncGenerator[dict, None]:
    """Canonical Finengine DEV registry pipeline used by UI and benchmarks."""
    async for event in _extract_stream_full_context(
        doc_id,
        company_type,
        index=index,
        limit=limit,
        model_id=model_id,
        include_segments=include_segments,
        include_consolidated_group=include_consolidated_group,
        metric_enums=metric_enums,
        full_registry=True,
        role=role,
        segment_mode=segment_mode,
        segment_context_mode=segment_context_mode,
        debug_dir=debug_dir,
        include_top_borrowers=include_top_borrowers,
    ):
        yield event


async def _extract_stream(
    doc_id: str,
    company_type: str,
    index=None,
    limit: Optional[int] = None,
    parallel: int = 8,
    model_id: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """Legacy per-metric RAG extraction (kept for fallback / comparison).

    The default production path is `_extract_stream_full_context` which sends
    the whole PDF in one shot to the LLM and returns a JSON of values.

    Args:
      limit: if set, extract only the first N extractable metrics (for quick UI tests).
      parallel: max concurrent Claude subprocess calls. Default 4.

    Event shapes:
      {"type": "start",  "total_metrics": N, "company_type": "...", "unit": "...", ...}
      {"type": "progress", "enum": "...", "name_ru": "..."}
      {"type": "metric", ...}
      {"type": "done",   ...}
    """
    if index is None:
        from finengine.ingestion.indexer import VectorIndex
        index = VectorIndex()

    doc = index.get_document(doc_id)
    if not doc:
        yield {"type": "error", "message": f"Document {doc_id} not found"}
        return

    fin_table_chunks = index.get_chunks_by_type("financial_table", doc_id=doc_id)

    # E-15 (2026-05-04): IDP-OCR upload writes only the .txt sidecar and skips
    # PyMuPDF parse_pdf, leaving FAISS chunks empty for the doc. Per-metric RAG
    # extraction can't work without those chunks (it'd retrieve nothing and
    # return null for everything). Backfill them lazily on first use:
    # call parse_pdf, index the result. ~30-60s lag on first extract per doc;
    # subsequent extracts hit the cache. Kept inside _extract_stream rather
    # than upload because most users use full-context arches and never need
    # this index.
    if not fin_table_chunks:
        text_chunks_total = index.get_chunks_by_type("text", doc_id=doc_id)
        if not text_chunks_total:
            from pathlib import Path as _Path
            from finengine.config import settings as _s
            from finengine.ingestion.pdf_parser import parse_pdf as _parse
            pdf_path = next(
                (p for p in _Path(_s.docs_dir).glob(f"{doc_id}*.pdf") if not p.name.endswith(".idpocr.v2.json")),
                None,
            )
            if pdf_path and pdf_path.exists():
                try:
                    new_chunks = _parse(pdf_path, doc, _s.chunk_size, _s.chunk_overlap)
                    if new_chunks:
                        index.add_document(doc, new_chunks)
                        fin_table_chunks = index.get_chunks_by_type("financial_table", doc_id=doc_id)
                except Exception as _e:
                    import logging as _log
                    _log.getLogger(__name__).warning(
                        "E-15 FAISS backfill failed for %s: %s", doc_id, _e
                    )

    effective_type = company_type
    if company_type in ("unknown", "auto", ""):
        effective_type = _detect_company_type_from_chunks(fin_table_chunks)

    if effective_type == "bank":
        extractable = EXTRACTABLE_BANK
        calculated_defs = CALCULATED_BANK
    else:
        extractable = EXTRACTABLE_NON_FINANCIAL
        calculated_defs = CALCULATED_NON_FINANCIAL

    if limit is not None and limit > 0:
        extractable = extractable[:limit]
        calculated_defs = []  # skip calculated when running a preview

    formula_default_abs_enums = {
        str(m.get("enum", "")).strip().upper()
        for m in extractable
        if m.get("formula_default_abs_behavior")
    }

    unit = _detect_unit(fin_table_chunks) or doc.period or ""
    period_current, period_prev = _detect_periods(fin_table_chunks)
    if doc.period and period_current == "Текущий":
        period_current = doc.period
        period_prev = str(int(doc.period) - 1) if doc.period.isdigit() else "Предыдущий"

    total_metrics = len(extractable) + len(calculated_defs)

    yield {
        "type": "start",
        "total_metrics": total_metrics,
        "total_extractable": len(extractable),
        "company_type": effective_type,
        "unit": unit,
        "period_current": period_current,
        "period_prev": period_prev,
    }

    # ── Per-metric extraction (parallel) ──────────────────────────────────
    extracted_current: dict[str, Optional[float]] = {}
    extracted_prev: dict[str, Optional[float]] = {}

    event_q: asyncio.Queue = asyncio.Queue()
    sem = asyncio.Semaphore(max(1, parallel))

    async def run_metric(metric: dict) -> None:
        enum = metric["enum"]
        async with sem:
            await event_q.put({"type": "progress", "enum": enum, "name_ru": metric["name_ru"]})
            chunks = _retrieve_chunks_for_metric(metric, index, doc_id, fin_table_chunks)

            value_current: Optional[float] = None
            value_prev: Optional[float] = None
            metric_unit_raw: Optional[str] = None

            if chunks:
                prompt = _build_metric_prompt(metric, chunks, period_current, period_prev)
                raw = ""
                for attempt in range(3):
                    raw = await _call_claude_metric(prompt, model_id=model_id)
                    if raw.strip():
                        break
                    await asyncio.sleep(2 * (attempt + 1))
                value_current, value_prev, metric_unit_raw = _pick_values_and_unit(raw)

            # Normalise to canonical "млн руб." so consumers (chains, quick
            # synthesis, calculated formulas, ratios) operate on a single
            # consistent scale. If the model didn't report a unit we trust
            # the global `unit` heuristic from `_detect_unit(chunks)`.
            canonical_unit = unit or "млн руб."
            effective_unit = metric_unit_raw or unit
            cur_norm, norm_unit = _normalise_money_value(value_current, effective_unit)
            prev_norm, _ = _normalise_money_value(value_prev, effective_unit)
            if norm_unit:
                canonical_unit = norm_unit
            value_current = cur_norm
            value_prev = prev_norm

            extracted_current[enum] = value_current
            extracted_prev[enum] = value_prev

            # Source page = first non-zero `page` of the retrieved chunks the
            # LLM saw. Lets the UI fill the «Источник» column instead of "—".
            source_page: Optional[int] = None
            if value_current is not None or value_prev is not None:
                for c in chunks:
                    p = getattr(c, "page", 0) or 0
                    if p > 0:
                        source_page = p
                        break

            await event_q.put({
                "type": "metric",
                "enum": enum,
                "name_ru": metric["name_ru"],
                "value_current": value_current,
                "value_prev": value_prev,
                "unit": canonical_unit,
                "found": value_current is not None,
                "is_calculated": False,
                "formula": "",
                "page": source_page,
            })

    tasks = [asyncio.create_task(run_metric(m)) for m in extractable]

    # Sentinel-on-completion pattern: when all tasks finish, push None.
    async def _fan_in_done() -> None:
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await event_q.put(None)

    fan_in = asyncio.create_task(_fan_in_done())

    try:
        while True:
            ev = await event_q.get()
            if ev is None:
                break
            yield ev
    finally:
        # Client aborted: cancel remaining work so subprocesses don't leak.
        for t in tasks:
            if not t.done():
                t.cancel()
        fan_in.cancel()

    # ── Calculated metrics ────────────────────────────────────────────────
    calc_current, calc_prev = _calculate_metrics_pair(
        calculated_defs,
        extracted_current,
        extracted_prev,
        formula_default_abs_enums=formula_default_abs_enums,
    )

    for enum, name_ru, formula, _deps in calculated_defs:
        v_cur = calc_current.get(enum)
        v_prv = calc_prev.get(enum)
        is_pct = "* 100" in formula or "× 100" in formula
        yield {
            "type": "metric",
            "enum": enum,
            "name_ru": name_ru,
            "value_current": v_cur,
            "value_prev": v_prv,
            "unit": "%" if is_pct else unit,
            "found": v_cur is not None,
            "is_calculated": True,
            "formula": formula,
        }

    yield {
        "type": "done",
        "period_current": period_current,
        "period_prev": period_prev,
        "unit": unit,
        "company_type": effective_type,
        "company_name": doc.company_name,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Backward-compatible non-streaming wrapper
# ─────────────────────────────────────────────────────────────────────────────

async def _extract_async(doc_id: str, company_type: str, index=None, parallel: int = 8) -> dict:
    """Collect all streaming events into a single result dict."""
    if index is None:
        from finengine.ingestion.indexer import VectorIndex
        index = VectorIndex()

    doc = index.get_document(doc_id)
    if not doc:
        return {"error": f"Document {doc_id} not found in index"}

    metrics: list[dict] = []
    meta: dict = {}

    async for event in _extract_stream_full_context(doc_id, company_type, index=index):
        etype = event.get("type")
        if etype == "error":
            return {"error": event.get("message", "unknown error")}
        elif etype == "metric":
            metrics.append({
                "enum": event["enum"],
                "name_ru": event["name_ru"],
                "value_current": event["value_current"],
                "value_prev": event["value_prev"],
                "period_values": event.get("period_values") or [],
                "unit": event["unit"],
                "found": event["found"],
                "is_calculated": event["is_calculated"],
                "formula": event.get("formula", ""),
                "segment": event.get("segment", ""),
                "dimension": event.get("dimension", ""),
            })
        elif etype == "done":
            meta = event

    return {
        "doc_id": doc_id,
        "company_name": doc.company_name,
        "period": doc.period,
        "company_type": meta.get("company_type", ""),
        "period_current": meta.get("period_current", "Текущий"),
        "period_prev": meta.get("period_prev", "Предыдущий"),
        "unit": meta.get("unit", ""),
        "metrics": metrics,
    }


def extract_all_sync(doc_id: str, company_type: str = "unknown") -> dict:
    """Synchronous wrapper (for scripts / tests)."""
    return asyncio.run(_extract_async(doc_id, company_type))
