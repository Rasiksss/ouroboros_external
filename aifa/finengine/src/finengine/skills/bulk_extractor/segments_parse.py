"""Разбор строк разрезов из текста — детерминированно, без модели.

Подстраховка стадии разрезов: если модель промолчала, часть строк вытягивается
из текста построчным разбором. Решений по смыслу здесь нет, только форма.
"""
from __future__ import annotations

import re


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

def _deterministic_segment_rows_from_text(text: str, standard: str) -> list[dict]:
    """Extract obvious line-wise segment tables when the LLM returns no rows."""
    if standard != "us_gaap_en":
        return []
    src = text or ""
    rows: list[dict] = []
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
                section_stops=("Segment cost of sales", "The following table provides items", "The Company does not"),
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
