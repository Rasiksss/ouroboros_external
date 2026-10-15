from __future__ import annotations

import argparse
import csv
import json
import math
import re
import unicodedata
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

DEFAULT_SCORE_THRESHOLD = 65.0

# ----------------------------- IO / OCR helpers -----------------------------

def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _ocr_payload(doc: dict[str, Any]) -> dict[str, Any]:
    if isinstance(doc.get("ocr_result"), dict):
        return doc["ocr_result"]
    return doc


def _tables(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tables = payload.get("tables") or []
    return [t for t in tables if isinstance(t, dict)]


def _table_body(table: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("caption", "annotation", "formatted_str", "text", "markdown"):
        v = table.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    return "\n".join(parts)


def _norm_spaces(s: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", s or "")


def _norm_path(p: str | Path) -> str:
    return unicodedata.normalize("NFC", str(p).replace("\\", "/"))


# ----------------------------- clean context helpers -----------------------------

def _line_spans_by_newlines(text: str) -> list[tuple[int, int]]:
    """
    OCR line/paragraph spans. A boundary is a run of 1..4 newline chars as requested.
    Longer runs still create boundaries through repeated matches in practice, but the
    1..4 rule is what matters for table-neighbor context.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for m in re.finditer(r"\n{1,4}", text):
        end = m.start()
        if end > start:
            spans.append((start, end))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _valid_position_range(table: dict[str, Any], full_text_len: int) -> tuple[int, int] | None:
    pr = table.get("position_range")
    if not (isinstance(pr, list) and len(pr) >= 2 and all(isinstance(x, int) for x in pr[:2])):
        return None
    start, end = max(0, pr[0]), min(full_text_len, pr[1])
    if end <= start:
        return None
    return start, end


def _table_ranges(tables: list[dict[str, Any]], full_text_len: int) -> list[tuple[int, int, int]]:
    ranges: list[tuple[int, int, int]] = []
    for idx, table in enumerate(tables):
        pr = _valid_position_range(table, full_text_len)
        if pr is not None:
            ranges.append((pr[0], pr[1], idx))
    return sorted(ranges)


def _span_overlaps_range(span: tuple[int, int], range_: tuple[int, int]) -> bool:
    a, b = span
    start, end = range_
    return a < end and b > start


def _span_contains_table(span: tuple[int, int], ranges: list[tuple[int, int, int]], current_idx: int | None = None) -> bool:
    for start, end, idx in ranges:
        if current_idx is not None and idx == current_idx:
            continue
        if _span_overlaps_range(span, (start, end)):
            return True
    return False


def _line_index_for_position(spans: list[tuple[int, int]], pos: int) -> int:
    import bisect
    starts = [a for a, _ in spans]
    return max(0, bisect.bisect_right(starts, pos) - 1)


def _line_table_flags(spans: list[tuple[int, int]], ranges: list[tuple[int, int, int]]) -> list[bool]:
    """Return True for OCR lines that intersect any table range. Single-pass, file-level."""
    flags = [False] * len(spans)
    if not spans or not ranges:
        return flags
    j = 0
    for i, (a, b) in enumerate(spans):
        while j < len(ranges) and ranges[j][1] <= a:
            j += 1
        k = j
        while k < len(ranges) and ranges[k][0] < b:
            if a < ranges[k][1] and b > ranges[k][0]:
                flags[i] = True
                break
            k += 1
    return flags


def _clean_lines_around_table(
    full_text: str,
    table: dict[str, Any],
    table_index: int,
    spans: list[tuple[int, int]],
    ranges: list[tuple[int, int, int]],
    pre_lines: int,
    post_lines: int,
    line_is_table: list[bool] | None = None,
) -> tuple[list[str], list[str]]:
    """
    Return the first N non-table OCR lines above and below the current table.
    If a nearby line intersects any neighboring OCR table by position_range, skip it
    and do not count it toward N.
    """
    pr = _valid_position_range(table, len(full_text))
    if pr is None or not spans:
        return [], []
    start, end = pr
    first_i = _line_index_for_position(spans, start)
    last_i = _line_index_for_position(spans, end)

    pre: list[str] = []
    i = first_i - 1
    while i >= 0 and len(pre) < pre_lines:
        span = spans[i]
        if not (line_is_table[i] if line_is_table is not None else _span_contains_table(span, ranges, current_idx=table_index)):
            line = full_text[span[0]:span[1]].strip()
            if line:
                pre.append(line)
        i -= 1
    pre.reverse()

    post: list[str] = []
    i = last_i + 1
    while i < len(spans) and len(post) < post_lines:
        span = spans[i]
        if not (line_is_table[i] if line_is_table is not None else _span_contains_table(span, ranges, current_idx=table_index)):
            line = full_text[span[0]:span[1]].strip()
            if line:
                post.append(line)
        i += 1
    return pre, post


def _nearest_title(pre_lines: list[str], table: dict[str, Any], title_lines: int = 4) -> str:
    cap = table.get("caption") if isinstance(table.get("caption"), str) else ""
    ann = table.get("annotation") if isinstance(table.get("annotation"), str) else ""
    # Use only the nearest lines above; far context is useful but should not dominate.
    nearest = pre_lines[-title_lines:] if title_lines > 0 else []
    return "\n".join(x for x in (cap, ann, *nearest) if isinstance(x, str) and x.strip())


# ----------------------------- patterns -----------------------------

RU_LOAN = re.compile(
    r"кредит[а-яё\s,./:;()\-]{0,55}(клиент|покупател|портфел|аванс|задолж|экспозиц)|"
    r"(портфел|структур)[а-яё\s,./:;()\-]{0,30}кредит|"
    r"кредиты\s+и\s+авансы\s+клиент|"
    r"ссудн[а-яё\s,./:;()\-]{0,30}задолж|"
    r"требован[а-яё\s,./:;()\-]{0,35}клиент",
    re.I,
)
EN_LOAN = re.compile(
    r"loans?\s+and\s+advances\s+to\s+customers?|"
    r"customer\s+loans?|loan\s+portfolio|loans?\s+and\s+leases|"
    r"commercial\s+loans?|commercial\s+and\s+industrial\s+loans?|\bC&I\s+loans?\b|"
    r"wholesale\s+credit\s+exposure|commercial\s+credit\s+exposure|credit\s+exposure|"
    r"corporate\s+credit\s+portfolio|corporate\s+loans?|"
    r"lending\s+exposure|lending\s+commitments?|funded\s+loans?|loans?\s+outstanding|"
    r"LHFI|total\s+loans\s+and\s+leases",
    re.I,
)
RU_AXIS = re.compile(
    r"по\s+(секторам|отраслям)\s+экономик|"
    r"в\s+разрезе\s+отрасл|отраслев[а-яё\s\-]{0,25}(структур|разрез|сектор)|"
    r"сектор[а-яё\s\-]{0,25}экономик|вид[а-яё\s\-]{0,15}экономическ[а-яё\s\-]{0,20}деятельност|"
    r"класс[а-яё\s\-]{0,20}экономическ[а-яё\s\-]{0,20}деятельност",
    re.I,
)
EN_AXIS = re.compile(
    r"by\s+industr(y|ies)|industry\s+sector[s]?|economic\s+sector[s]?|"
    r"economic\s+activit(y|ies)|industry\s+concentration[s]?|"
    r"industry\s+classification|classification\s+of\s+economic\s+activities|"
    r"\bNAICS\b|\bNACE\b|\bANZSIC\b",
    re.I,
)

# Explicit high-value target titles. These are general labels, not company-specific.
STRONG_TARGET_TITLE = re.compile(
    r"commercial\s+(and\s+industrial\s+)?loans?\s+by\s+industry|"
    r"commercial\s+loans?\s+by\s+industry|"
    r"loans?\s+and\s+leases\s+by\s+industry\s+sector|"
    r"loans?\s+and\s+advances\s+to\s+customers?.{0,80}(by|analysed|analyzed).{0,80}(industry|sector|economic)|"
    r"(credit\s+risk\s+)?concentration.{0,80}(loans?|credit\s+exposure).{0,80}(industry|sector|economic)|"
    r"(credit\s+risk\s+)?concentration.{0,80}(industry|sector|economic).{0,80}(loans?|credit\s+exposure)|"
    r"wholesale\s+credit\s+exposure\s*[-–—:]?\s*industr(y|ies)|"
    r"industry\s+concentration[s]?.{0,80}(commercial\s+credit|credit\s+exposure|loans?)|"
    r"corporate\s+credit\s+portfolio.{0,80}industry|"
    r"corporate\s+lending.{0,80}industry|"
    r"lending\s+exposure.{0,80}industry|"
    r"концентрац[а-яё\s,./:;()\-]{0,50}риск[а-яё\s,./:;()\-]{0,90}(кредит|портфел|клиент)[а-яё\s,./:;()\-]{0,90}(сектор|отрасл)|"
    r"концентрац[а-яё\s,./:;()\-]{0,50}риск[а-яё\s,./:;()\-]{0,90}(сектор|отрасл)[а-яё\s,./:;()\-]{0,90}(кредит|портфел|клиент)|"
    r"кредит[а-яё\s,./:;()\-]{0,50}(портфел|клиент|аванс)[а-яё\s,./:;()\-]{0,80}по\s+(секторам|отраслям)|"
    r"кредиты\s+и\s+авансы\s+клиент[а-яё\s,./:;()\-]{0,80}по\s+(секторам|отраслям)|"
    r"портфел[а-яё\s,./:;()\-]{0,30}кредит[а-яё\s,./:;()\-]{0,80}по\s+(секторам|отраслям)",
    re.I | re.S,
)

CONCENTRATION = re.compile(r"concentration[s]?|концентрац", re.I)
RISK = re.compile(r"credit\s+risk|risk\s+exposure|риск|credit\s+quality", re.I)
TOTAL = re.compile(r"\btotal\b|\bsubtotal\b|итого|всего|100\s*%", re.I)
NUMERIC = re.compile(r"(20\d{2}|31\s+dec|30\s+jun|\$|€|£|₽|%|млн|тыс|million|thousand|in\s+millions)", re.I)

INDUSTRY_PATTERNS = [
    # Russian rows
    r"физическ\w+\s+лица", r"частн\w+\s+лица", r"строительств", r"нефт\w*\s+и\s+газ|нефтегаз", r"металлург", r"транспорт",
    r"торгов", r"коммерц", r"уголь", r"энергет", r"химическ", r"пищев", r"сельск", r"агро",
    r"финанс", r"лизинг", r"машиностро", r"телеком", r"средств\w+\s+массов", r"государствен",
    r"недвижим", r"обрабатывающ", r"производств", r"промышлен", r"добыч", r"здравоохран", r"образован",
    r"услуг", r"прочее",
    # English rows
    r"retail", r"wholesale", r"trade", r"finance", r"insurance", r"real\s+estate",
    r"manufactur", r"construction", r"health\s*care|healthcare", r"transport", r"storage\s+services", r"technology",
    r"telecommunication", r"media", r"communication", r"energy", r"oil\s*,?\s*gas|oil\s+and\s+gas", r"natural\s+resources",
    r"utilit", r"agriculture", r"forestry", r"fisher", r"public\s+administration",
    r"government", r"education", r"sovereign", r"individuals?", r"households?", r"consumer", r"food",
    r"chemical", r"metals?", r"mining", r"accommodation", r"professional\s+services",
    r"leisure", r"hospitality", r"automotive", r"commercial\s+services", r"asset\s+managers", r"capital\s+goods",
    r"diversified\s+industrials", r"financial\s+institutions", r"funds", r"materials", r"other",
]
INDUSTRY_RE = re.compile("|".join(f"(?:{p})" for p in INDUSTRY_PATTERNS), re.I)

# Table/current-context negative signals.
NEG_DEPOSIT_REVENUE = re.compile(
    r"customer\s+deposits\s+by\s+industry|deposits\s+by\s+industry|"
    r"вклады|депозит[а-яё\s\-]{0,30}(по\s+отраслям|по\s+секторам)|"
    r"suppliers?\s+by\s+industry|revenue\s+by\s+industry|sales\s+by\s+industry|"
    r"segment\s+revenue|выручк[а-яё\s\-]{0,30}(по\s+сегмент|по\s+отрас)",
    re.I,
)
NEG_PROPERTY_GEO = re.compile(
    r"commercial\s+real\s+estate.{0,150}(property\s+type|geograph|state)|"
    r"property\s+type\s+and\s+state|geography\s+and\s+property\s+type|"
    r"permanent\s+finance\s+by\s+property\s+type|"
    r"apartment[s]?/multifamily|office\s+\| |hotel\s+\| |retail/service|"
    r"geographic\s+location|by\s+state|states?\s+and\s+property",
    re.I | re.S,
)
NEG_DERIVATIVE = re.compile(r"OTC\s+derivative|derivative\s+assets?|net\s+credit\s+exposure.{0,80}derivative", re.I | re.S)
NEG_ALLOWANCE = re.compile(r"allowance\s+for\s+credit\s+loss|\bACLL\b|allowance\s+coverage|provision\s+for\s+credit\s+loss|резерв[а-яё\s\-]{0,30}ожидаем", re.I)
NEG_RELIEF = re.compile(r"COVID|relief|forbearance|deferral|deferred\s+principal|modification\s+program", re.I)
NEG_ASSET_WIDE = re.compile(
    r"отраслев[а-яё\s\-]{0,30}структур[а-яё\s\-]{0,30}актив|"
    r"структур[а-яё\s\-]{0,30}актив[а-яё\s\-]{0,60}(по\s+отраслям|по\s+секторам)|"
    r"assets?\s+by\s+industr(y|ies)|distribution\s+of\s+assets?\s+by\s+industr|financial\s+assets?.{0,80}by\s+industr",
    re.I | re.S,
)

QUICK_RELEVANCE = re.compile(
    r"loan|credit|lending|exposure|commercial|industry|industries|sector|economic|classification|"
    r"кредит|ссуд|задолж|экспозиц|риск|концентрац|отрасл|сектор|экономическ|портфел",
    re.I,
)

ASSET_CLASS_COLUMNS = re.compile(
    r"cash\s+and\s+cash\s+equivalents|due\s+from\s+banks|securities|investment\s+securities|"
    r"derivative\s+financial\s+assets|прочие\s+активы|ценные\s+бумаги|денежные\s+средства|средства\s+в\s+банках",
    re.I,
)


def _industry_hit_count(text: str) -> int:
    hits: set[str] = set()
    for m in INDUSTRY_RE.finditer(text):
        s = re.sub(r"\s+", " ", m.group(0).lower()).strip()
        if len(s) >= 4:
            hits.add(s[:28])
    return len(hits)


def _numeric_table_quality(table_text: str) -> tuple[int, list[str]]:
    reasons: list[str] = []
    if not NUMERIC.search(table_text):
        return 0, reasons
    num_count = len(re.findall(r"(?<!\w)[\(\-]?(?:\d{1,3}(?:[ ,.\u00a0]\d{3})+|\d+)(?:[,.]\d+)?\s*%?(?!\w)", table_text))
    row_like = table_text.count("\n") >= 3 or table_text.count("|") >= 8
    if row_like and num_count >= 10:
        reasons.append(f"numeric_table(num={num_count})")
        return 6, reasons
    if row_like and num_count >= 5:
        reasons.append(f"weak_numeric_table(num={num_count})")
        return 3, reasons
    return 0, reasons


@dataclass
class RankResult:
    file: str
    standard: str
    company_type: str
    table_index: int
    rank: int
    page: int | None
    score: float
    reasons: list[str]
    penalties: list[str]
    caption: str
    snippet: str


def score_table(
    full_text: str,
    table: dict[str, Any],
    table_index: int,
    spans: list[tuple[int, int]],
    ranges: list[tuple[int, int, int]],
    line_is_table: list[bool] | None = None,
    pre_lines: int = 10,
    post_lines: int = 2,
    title_lines: int = 4,
) -> tuple[float, list[str], list[str], str]:
    table_text = _norm_spaces(_table_body(table))
    pre, post = _clean_lines_around_table(full_text, table, table_index, spans, ranges, pre_lines, post_lines, line_is_table=line_is_table)
    title = _norm_spaces(_nearest_title(pre, table, title_lines=title_lines))
    # For very strong title patterns, avoid treating a long narrative paragraph as
    # a table title. Keep caption/annotation plus the nearest short/table-like lines.
    cap = table.get("caption") if isinstance(table.get("caption"), str) else ""
    ann = table.get("annotation") if isinstance(table.get("annotation"), str) else ""
    actual_title_lines = [x for x in (cap, ann) if x and x.strip()]
    for line in pre[-title_lines:]:
        low = line.lower()
        if len(line) <= 320 or "table" in low or "таблиц" in low or "**" in line:
            actual_title_lines.append(line)
    actual_title = _norm_spaces("\n".join(actual_title_lines))
    wider_pre = _norm_spaces("\n".join(pre))
    post_text = _norm_spaces("\n".join(post))
    combined_wide = "\n".join(x for x in (wider_pre, table_text, post_text) if x.strip())

    # Fast reject for the overwhelming majority of OCR tables. It is intentionally
    # broad: it only removes tables with no lexical relation to loans/credit/industry.
    if not QUICK_RELEVANCE.search(combined_wide[:5000]):
        return -100.0, [], ["no quick relevance signal"], re.sub(r"\s+", " ", combined_wide).strip()[:1000]

    score = 0.0
    reasons: list[str] = []
    penalties: list[str] = []

    numeric_score, numeric_reasons = _numeric_table_quality(table_text)
    score += numeric_score
    reasons.extend(numeric_reasons)

    row_hits = _industry_hit_count(table_text)
    if row_hits:
        # Industry-like rows are necessary but not sufficient; cap prevents giant generic tables from dominating.
        add = min(row_hits, 12) * 2.0
        score += add
        reasons.append(f"industry_rows={row_hits}(+{add:g})")

    strong_title = bool(STRONG_TARGET_TITLE.search(actual_title) or STRONG_TARGET_TITLE.search(table_text[:1200]))
    if strong_title:
        score += 45
        reasons.append("strong_target_title(+45)")

    title_has_loan = bool(RU_LOAN.search(title) or EN_LOAN.search(title))
    table_has_loan = bool(RU_LOAN.search(table_text) or EN_LOAN.search(table_text))
    wide_has_loan = bool(RU_LOAN.search(combined_wide) or EN_LOAN.search(combined_wide))
    if title_has_loan:
        score += 22
        reasons.append("loan/lending in near title(+22)")
    elif table_has_loan:
        score += 16
        reasons.append("loan/lending in table(+16)")
    elif wide_has_loan:
        score += 9
        reasons.append("loan/lending in wider context(+9)")

    title_has_axis = bool(RU_AXIS.search(title) or EN_AXIS.search(title))
    table_has_axis = bool(RU_AXIS.search(table_text) or EN_AXIS.search(table_text))
    wide_has_axis = bool(RU_AXIS.search(combined_wide) or EN_AXIS.search(combined_wide))
    if title_has_axis:
        score += 22
        reasons.append("industry axis in near title(+22)")
    elif table_has_axis:
        score += 15
        reasons.append("industry axis in table(+15)")
    elif wide_has_axis:
        score += 8
        reasons.append("industry axis in wider context(+8)")

    # Evidence from column headers: helps US GAAP tables where title is weak but body has exposure/outstanding/commitments.
    exposure_cols = bool(re.search(
        r"total\s+credit\s+exposure|credit\s+exposure|funded\s+exposure|unfunded|"
        r"loans?\s+outstanding|outstanding\s+loans?|total\s+commitments?|lending\s+commitments?|"
        r"%\s+of\s+total\s+loans|%\s+of\s+total\s+loans\s+and\s+leases|\bLHFI\b|"
        r"кредиты\s+клиентам|кредиты\s+и\s+авансы|сумма\s+%|сумма",
        table_text,
        re.I,
    ))
    if exposure_cols:
        score += 14
        reasons.append("loan/exposure columns(+14)")

    if CONCENTRATION.search(title) or CONCENTRATION.search(combined_wide):
        score += 7
        reasons.append("concentration keyword(+7)")
    if RISK.search(title) or RISK.search(combined_wide):
        score += 5
        reasons.append("risk/credit-quality keyword(+5)")
    if TOTAL.search(table_text):
        score += 5
        reasons.append("total row(+5)")

    # Specific weak-but-valid cases:
    # - Capital One-like: percentage-only commercial loans by industry.
    # - JPM/Citi-like: table body has Credit exposure + many industries; heading may be one line above.
    if re.search(r"industry\s+classification", table_text, re.I) and row_hits >= 5 and (wide_has_loan or title_has_loan):
        score += 18
        reasons.append("percentage-only industry classification with loan context(+18)")
    if re.search(r"credit\s+exposure", table_text, re.I) and row_hits >= 8 and wide_has_axis:
        score += 18
        reasons.append("credit exposure body with industry axis context(+18)")
    if re.search(r"loans?\s+to\s+customers\s+at\s+amorti[sz]ed\s+cost", table_text, re.I) and row_hits >= 6:
        score += 25
        reasons.append("IFRS loans to customers at amortized cost body(+25)")

    # Penalties: applied to the current table/title, not the whole wide context, to avoid suppressing a true table
    # because a neighboring table is mentioned in surrounding narrative.
    current_scope = "\n".join(x for x in (title, table_text[:2500]) if x.strip())

    if NEG_DEPOSIT_REVENUE.search(current_scope):
        score -= 70
        penalties.append("deposit/revenue/supplier table(-70)")
    has_explicit_industry_panel = bool(re.search(r"\bBy Industry\b|Industry\s*:|Industry\s+Classification|Industry\s+sector|отрасл|сектор", table_text, re.I))
    if NEG_PROPERTY_GEO.search(current_scope) and not has_explicit_industry_panel:
        score -= 45
        penalties.append("property/geography table(-45)")
    if NEG_DERIVATIVE.search(current_scope) and not re.search(r"lending|loans?\s+outstanding|corporate\s+lending", current_scope, re.I):
        score -= 45
        penalties.append("derivative exposure not lending(-45)")
    if NEG_ALLOWANCE.search(current_scope) and not strong_title:
        score -= 32
        penalties.append("allowance/provision derived table(-32)")
    if NEG_RELIEF.search(current_scope) and not strong_title:
        score -= 30
        penalties.append("COVID/relief subset(-30)")
    wide_asset_has_target_evidence = (
        row_hits >= 8
        and (title_has_axis or table_has_axis or exposure_cols or strong_title)
        and (title_has_loan or table_has_loan or wide_has_loan)
    )
    if (
        NEG_ASSET_WIDE.search(current_scope)
        and ASSET_CLASS_COLUMNS.search(table_text)
        and not re.search(r"loans?\s+to\s+customers\s+at\s+amorti[sz]ed\s+cost", table_text, re.I)
        and not wide_asset_has_target_evidence
    ):
        score -= 55
        penalties.append("wide assets-by-industry table(-55)")
    elif wide_asset_has_target_evidence:
        reasons.append("wide asset table has target loan/industry evidence(no penalty)")

    # If the table itself has almost no industry axis/rows, do not let a surrounding
    # narrative about concentration limits push maturity/counterparty tables into top ranks.
    if row_hits < 5 and not table_has_axis and not re.search(r"\bBy Industry\b|Industry\s*:|Industry\s+Classification|Industry\s+sector|отрасл|сектор", table_text, re.I):
        score -= 40
        penalties.append("weak industry evidence inside table(-40)")

    # If no real lending/loan/credit-exposure context, suppress generic industry/revenue tables.
    if not (title_has_loan or table_has_loan or wide_has_loan or exposure_cols or strong_title):
        score -= 25
        penalties.append("no loan/lending/exposure signal(-25)")

    snippet = re.sub(r"\s+", " ", combined_wide).strip()[:1000]
    return score, reasons, penalties, snippet


def _standard_and_company_type(rel_file: str) -> tuple[str, str]:
    parts = _norm_path(rel_file).split("/")
    standard = "IFRS" if parts and parts[0] == "МСФО" else ("US_GAAP" if parts and parts[0] == "US_GAAP" else "UNKNOWN")
    company_type = "Кредитные" if "Кредитные" in parts else ("Нефинансовые" if "Нефинансовые" in parts else "UNKNOWN")
    return standard, company_type


def rank_file(
    path: Path,
    root: Path,
    pre_lines: int = 10,
    post_lines: int = 2,
    title_lines: int = 4,
    min_score: float | None = DEFAULT_SCORE_THRESHOLD,
) -> list[RankResult]:
    doc = _read_json(path)
    if not doc:
        return []
    payload = _ocr_payload(doc)
    rel = _norm_path(path.relative_to(root))
    return rank_payload(
        payload,
        rel,
        pre_lines=pre_lines,
        post_lines=post_lines,
        title_lines=title_lines,
        min_score=min_score,
    )


def rank_payload(
    payload: dict[str, Any],
    rel_file: str = "document.json",
    *,
    pre_lines: int = 10,
    post_lines: int = 2,
    title_lines: int = 4,
    min_score: float | None = DEFAULT_SCORE_THRESHOLD,
) -> list[RankResult]:
    """Rank tables from an already loaded OCR payload.

    Production extraction already has the IDP-OCR JSON sidecar available; this
    entrypoint avoids writing temp files just to use the ranking logic.
    """
    full_text = payload.get("text") if isinstance(payload.get("text"), str) else ""
    tables = _tables(payload)
    spans = _line_spans_by_newlines(full_text)
    ranges = _table_ranges(tables, len(full_text))
    line_is_table = _line_table_flags(spans, ranges)
    rel = _norm_path(rel_file)
    standard, company_type = _standard_and_company_type(rel)
    results: list[RankResult] = []
    for i, table in enumerate(tables):
        table_text = _table_body(table)
        if len(table_text.strip()) < 60:
            continue
        score, reasons, penalties, snippet = score_table(full_text, table, i, spans, ranges, line_is_table, pre_lines, post_lines, title_lines)
        if not math.isfinite(score):
            continue
        if min_score is not None and score < min_score:
            continue
        results.append(RankResult(
            file=rel,
            standard=standard,
            company_type=company_type,
            table_index=i,
            rank=0,
            page=table.get("page") if isinstance(table.get("page"), int) else None,
            score=round(score, 3),
            reasons=reasons,
            penalties=penalties,
            caption=(table.get("caption") or table.get("annotation") or "")[:300],
            snippet=snippet,
        ))
    results.sort(key=lambda r: (-r.score, r.table_index))
    for rank, r in enumerate(results, start=1):
        r.rank = rank
    return results


def _rank_file_worker(args: tuple[str, str, int, int, int, float | None]) -> list[RankResult]:
    path_s, root_s, pre_lines, post_lines, title_lines, min_score = args
    return rank_file(Path(path_s), Path(root_s), pre_lines, post_lines, title_lines, min_score)


def rank_dir(
    root: Path,
    pre_lines: int = 10,
    post_lines: int = 2,
    title_lines: int = 4,
    workers: int = 1,
    min_score: float | None = DEFAULT_SCORE_THRESHOLD,
) -> list[RankResult]:
    files = [p for p in sorted(root.rglob("*.json")) if "__MACOSX" not in p.parts and not p.name.startswith("._")]
    out: list[RankResult] = []
    if workers and workers > 1 and len(files) > 1:
        jobs = [(str(p), str(root), pre_lines, post_lines, title_lines, min_score) for p in files]
        with ProcessPoolExecutor(max_workers=workers) as ex:
            future_map = {ex.submit(_rank_file_worker, job): job[0] for job in jobs}
            for fut in as_completed(future_map):
                out.extend(fut.result())
    else:
        for path in files:
            out.extend(rank_file(path, root, pre_lines, post_lines, title_lines, min_score))
    out.sort(key=lambda r: (r.file, r.rank))
    return out


def write_outputs(results: list[RankResult], csv_out: Path | None, json_out: Path | None, top_per_doc: int = 10) -> None:
    # Keep only top N per document in files by default to keep output readable.
    by_file: dict[str, list[RankResult]] = {}
    for r in results:
        by_file.setdefault(r.file, []).append(r)
    limited = []
    for file in sorted(by_file):
        limited.extend(by_file[file][:top_per_doc])

    if json_out:
        json_out.write_text(json.dumps([asdict(r) for r in limited], ensure_ascii=False, indent=2), encoding="utf-8")
    if csv_out:
        with csv_out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(asdict(limited[0]).keys()) if limited else ["file"])
            writer.writeheader()
            for r in limited:
                d = asdict(r)
                d["reasons"] = "; ".join(d["reasons"])
                d["penalties"] = "; ".join(d["penalties"])
                writer.writerow(d)


def main() -> None:
    ap = argparse.ArgumentParser(description="Rank OCR tables for bank loan/credit-risk industry-sector disclosures.")
    ap.add_argument("root", type=Path, help="Directory with OCR JSONs. Use the directory that contains US_GAAP/ and МСФО/.")
    ap.add_argument("--pre-lines", type=int, default=10)
    ap.add_argument("--post-lines", type=int, default=2)
    ap.add_argument("--title-lines", type=int, default=4)
    ap.add_argument("--min-score", type=float, default=DEFAULT_SCORE_THRESHOLD)
    ap.add_argument("--top-per-doc", type=int, default=10)
    ap.add_argument("--workers", type=int, default=1, help="Use process workers for large directories.")
    ap.add_argument("--csv-out", type=Path)
    ap.add_argument("--json-out", type=Path)
    args = ap.parse_args()

    results = rank_dir(
        args.root,
        args.pre_lines,
        args.post_lines,
        args.title_lines,
        workers=args.workers,
        min_score=args.min_score,
    )
    write_outputs(results, args.csv_out, args.json_out, args.top_per_doc)
    print(json.dumps([asdict(r) for r in results[:20]], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
