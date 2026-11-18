"""Определение исходной формы отчётности по странице: баланс, ОПУ, ДДС, капитал, примечания.

Зачем: одно и то же название строки в балансе и в примечании означает разное.
Привязка показателя к форме отсекает совпадения из чужого раздела.
"""
from __future__ import annotations

import re


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
            "балансовый отчет",
            "бухгалтерский баланс",
            "statement of financial position",
            "statements of financial position",
            "balance sheet",
            "balance sheets",
            "statement of financial condition",
        ),
    ),
    (
        SOURCE_FORM_PNL,
        (
            "отчет о прибылях и убытках",
            "отчет о прибылях или убытках",
            "отчет о прибыли и убытке",
            "отчет о прибыли или убытке",
            "отчет о финансовых результатах",
            "отчет о совокупном доходе",
            "statement of profit or loss",
            "statements of profit or loss",
            "statement of comprehensive income",
            "statements of comprehensive income",
            "income statement",
            "statements of income",
            "statement of income",
            "statement of operations",
            "statement of financial results",
        ),
    ),
    (
        SOURCE_FORM_CASH_FLOW,
        (
            "отчет о движении денежных средств",
            "отчет о денежных потоках",
            "statement of cash flows",
            "statements of cash flows",
            "cash flow statement",
            "cash flows statement",
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
            "statement of changes in equity",
            "statements of changes in equity",
            "statement of shareholders equity",
            "statement of shareholders' equity",
            "statement of stockholders equity",
            "statement of stockholders' equity",
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
