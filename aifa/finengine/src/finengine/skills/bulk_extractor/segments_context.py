"""Что искать в разрезах и какой кусок документа подать модели.

Состав показателей ЗАВИСИТ ОТ ЯЧЕЙКИ (#252): у банка нет ни выручки, ни
операционной прибыли, ни EBITDA — этих enum нет в реестре `ifrs_bank_ru`
вообще. Спрашивать их у банка значило спрашивать то, чего в его сегментной
записке не бывает; замер дал 2 строки фоллбэка вместо 20 настоящих разрезов.

⚠️ Состав живёт здесь ВРЕМЕННО. Списки показателей — данные, их место в
реестре ячейки (#224/#250). Пока они в коде, их правка требует программиста.
"""
from __future__ import annotations

import re


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

# У банка нет ни выручки, ни операционной прибыли, ни EBITDA — этих enum нет в
# ячейке `ifrs_bank_ru` ВООБЩЕ. Спрашивать их в разрезах значило спрашивать то,
# чего в сегментной таблице банка не бывает: из пяти нефинансовых показателей у
# Ак Барса нашлись только NET_INCOME и INTEREST_PAID, и обе строки приехали не
# разрезом, а фоллбэком из основного извлечения. Состав ниже собран по строкам
# сегментной записки банка (МСФО (IFRS) 8): процентные доходы, комиссионные
# доходы, ЧПД, совокупные операционные доходы, итоговый результат.
_SEGMENT_METRICS_BANK_RU: set[str] = {
    "INTEREST_INCOME_ER",
    "FEE_AND_COMMISSION_INCOME",
    "NET_INTEREST_INCOME",
    "OPERATING_INCOME_BANKS",
    "NET_INCOME",
}

def _role_wants_segments(role: str | None) -> bool:
    r = (role or "").lower()
    return "диэ" in r or "die" in r

def _detect_reporting_standard(text: str) -> str:
    head = (text or "")[:120_000]
    low = head.lower()
    if "u.s. gaap" in low or "us gaap" in low or "united states securities" in low:
        return "us_gaap_en"
    # A cheap language fallback: enough for the current RU IFRS / EN US GAAP split.
    cyr = sum(1 for ch in head[:20_000] if "а" <= ch.lower() <= "я")
    lat = sum(1 for ch in head[:20_000] if "a" <= ch.lower() <= "z")
    return "ifrs_ru" if cyr >= lat * 0.35 else "us_gaap_en"

def _segment_metric_scope(
    metrics: list[dict], standard: str, company_type: str = "",
) -> list[dict]:
    if str(company_type or "").strip().lower() == "bank":
        wanted = _SEGMENT_METRICS_BANK_RU
    elif standard == "us_gaap_en":
        wanted = _SEGMENT_METRICS_US_GAAP_EN
    else:
        wanted = _SEGMENT_METRICS_IFRS_RU
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

def _segment_rules_en(segment_mode: str | None) -> str:
    mode = _normalize_segment_mode(segment_mode)
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
