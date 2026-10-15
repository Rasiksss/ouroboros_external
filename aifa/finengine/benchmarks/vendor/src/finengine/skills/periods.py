"""Period normalization for extraction outputs.

The extractor should not leak report-specific wording like "fiscal Q3" or
"year ended". It emits compact labels that downstream UI / benchmarks can join
on. `FY 2025` is reserved strictly for the calendar year ended 31.12.2025;
shifted fiscal years are represented as exact ended-date durations:

  * 5.11.2025         point-in-time value
  * FY 2025           year ended 31.12.2025
  * 12M 28.02.2025    year ended on a non-calendar date
  * Q1..Q4 2025       calendar quarter
  * 6M / 9M 2025      year-to-date calendar periods
  * 3M 21.02.2025     non-calendar rolling period ending on a date
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
import re

from finengine.data.metric_period_types import (
    DURATION_PERIOD_TYPE,
    POINT_IN_TIME_METRIC_ENUMS,
    POINT_PERIOD_TYPE,
    fallback_period_type_for_enum,
)

_RU_MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "ма": 5,  # май / мая
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}

_EN_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_MONTH_NAME_RE = (
    r"(?:январ[ьяе]?|феврал[ьяе]?|март[ае]?|апрел[ьяе]?|ма[йяе]|июн[ьяе]?|"
    r"июл[ьяе]?|август[ае]?|сентябр[ьяе]?|октябр[ьяе]?|ноябр[ьяе]?|декабр[ьяе]?|"
    r"January|Jan\.?|February|Feb\.?|March|Mar\.?|April|Apr\.?|May|June|Jun\.?|"
    r"July|Jul\.?|August|Aug\.?|September|Sept\.?|Sep\.?|October|Oct\.?|"
    r"November|Nov\.?|December|Dec\.?)"
)

_WORD_TO_MONTHS = {
    "one": 1, "один": 1,
    "three": 3, "три": 3, "трех": 3, "трёх": 3,
    "six": 6, "шесть": 6, "шести": 6,
    "nine": 9, "девять": 9, "девяти": 9,
    "twelve": 12, "двенадцать": 12, "двенадцати": 12,
}

_INCOMPLETE_DURATION_RE = re.compile(
    r"\b(?P<months>three|six|nine|twelve|3|6|9|12|три|тр[её]х|шесть|шести|"
    r"девять|девяти|двенадцать|двенадцати)\s+"
    r"(?:months?|мес(?:яцев|яца|\.?)?)\b[^\n|]{0,40}?\b(?P<year>20\d{2})\b",
    re.I,
)

_POINT_IN_TIME_RE = (
    r"\b(?:as\s+of|as\s+at|at|на\s+дату|по\s+состоянию|на\s+момент|"
    r"на\s+конец|на\s+отчетную\s+дату|на\s+отч[её]тную\s+дату|"
    r"balance\s+sheet|statement\s+of\s+financial\s+position)\b"
)


@dataclass(frozen=True)
class PeriodInfo:
    label: str
    end_date: date | None = None
    months: int | None = None
    is_point: bool = False


@dataclass(frozen=True)
class FiscalPeriodContext:
    """Document-level fiscal-year mapping.

    Some US GAAP reports use table headers like "2024" while defining near the
    beginning that "2024" means the fiscal year ended September 1, 2024. The
    LLM should still return the raw "2024" header; this context lets Python
    normalize that header to a shifted fiscal period only for this document.
    """

    year_end_dates: dict[int, date]

    @classmethod
    def empty(cls) -> "FiscalPeriodContext":
        return cls({})

    @classmethod
    def from_text(cls, text: object) -> "FiscalPeriodContext":
        return cls(_extract_fiscal_year_end_dates(str(text or "")))

    def end_date_for_year(self, year: int) -> date | None:
        return self.year_end_dates.get(int(year))

    def has_shifted_years(self) -> bool:
        return any((d.month, d.day) != (12, 31) for d in self.year_end_dates.values())

    def label_for_year(self, year: int, *, value_kind: str | None = None) -> str:
        end = self.end_date_for_year(year)
        if not end:
            return ""
        if _kind_is_point(value_kind):
            return _date_label(end)
        return _calendar_label_for_duration(12, end)


POINT_IN_TIME_ENUMS = POINT_IN_TIME_METRIC_ENUMS
POINT_KIND = POINT_PERIOD_TYPE
DURATION_KIND = DURATION_PERIOD_TYPE


def period_kind_for_enum(enum: object) -> str:
    """Return whether a metric is measured at a date or over a period.

    Runtime reads `period_type` from `metrics.json`. If a deployment still has
    an older registry without that field, use the data fallback from
    `metric_period_types.py`.
    """
    value = str(enum or "").strip().upper()
    try:
        from finengine.skills.metric_registry import METRIC_PERIOD_TYPE_BY_ENUM

        period_type = str(METRIC_PERIOD_TYPE_BY_ENUM.get(value) or "").strip().lower()
    except Exception:
        period_type = ""
    if period_type in {POINT_KIND, DURATION_KIND}:
        return period_type
    return fallback_period_type_for_enum(value)


def _kind_is_point(value_kind: str | None) -> bool:
    value = str(value_kind or "").strip().lower()
    return value in {"point", "date", "instant", "at_date", "as_of", "на", "на момент"}


def _kind_is_duration(value_kind: str | None) -> bool:
    value = str(value_kind or "").strip().lower()
    return value in {"duration", "period", "flow", "for_period", "за", "за период"}


def _months_token_to_int(token: object) -> int:
    text = str(token or "").strip().lower().replace("ё", "е")
    if text.isdigit():
        return int(text)
    return _WORD_TO_MONTHS.get(text, 0)


def _quarter_from_anchor_period(label: object) -> int | None:
    text = normalize_period_label(label)
    if m := re.fullmatch(r"Q([1-4]) 20\d{2}", text):
        return int(m.group(1))
    if m := re.fullmatch(r"([369])M 20\d{2}", text):
        months = int(m.group(1))
        return months // 3
    if m := re.fullmatch(r"(\d{1,2})M \d{1,2}[.](\d{1,2})[.]20\d{2}", text):
        months = int(m.group(1))
        if months in {3, 6, 9, 12}:
            return months // 3
    return None


def normalize_incomplete_duration_label(
    raw: object,
    anchor_periods: list[object] | tuple[object, ...] = (),
) -> str:
    """Normalize incomplete duration headers such as "За три месяца, 2024".

    Three-month labels without an end date are ambiguous. When the report
    context has Q3/9M current-period anchors, map the comparative three-month
    label to the same quarter in the previous year.
    """
    text = str(raw or "").replace("\u00a0", " ").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    if _parse_date(text) is not None:
        return ""
    match = _INCOMPLETE_DURATION_RE.search(text)
    if not match:
        return ""
    months = _months_token_to_int(match.group("months"))
    year = int(match.group("year"))
    if months == 3:
        quarter = next(
            (q for q in (_quarter_from_anchor_period(p) for p in anchor_periods) if q),
            None,
        )
        return f"Q{quarter} {year}" if quarter else ""
    if months in {6, 9}:
        return f"{months}M {year}"
    if months == 12:
        return f"FY {year}"
    return ""


def _month_from_word(word: str) -> int | None:
    w = word.lower().replace(".", "").strip()
    for prefix, month in _RU_MONTHS.items():
        if w.startswith(prefix):
            return month
    for prefix, month in _EN_MONTHS.items():
        if w.startswith(prefix):
            return month
    return None


def _two_or_four_digit_year(raw: object) -> int | None:
    text = str(raw or "").strip().replace("'", "").replace("’", "")
    if re.fullmatch(r"\d{2}", text):
        year = int(text)
        return 2000 + year if year <= 79 else 1900 + year
    if re.fullmatch(r"(?:19|20)\d{2}", text):
        return int(text)
    return None


def _month_end_date(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _label_from_month_year(month: int, year: int, *, value_kind: str | None = None) -> str:
    end = _month_end_date(year, month)
    if _kind_is_point(value_kind):
        return _date_label(end)
    if month == 3:
        return f"Q1 {year}"
    if month == 6:
        return f"6M {year}"
    if month == 9:
        return f"9M {year}"
    if month == 12:
        return f"FY {year}"
    return f"12M {_date_label(end)}"


def _label_from_quarter(q: int, year: int, *, value_kind: str | None = None) -> str:
    if _kind_is_point(value_kind):
        end_month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[q]
        return _date_label(date(year, *end_month_day))
    return f"Q{q} {year}"


def _strip_period_qualifiers(raw: object) -> str:
    text = str(raw or "").replace("\u00a0", " ").strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*\(\s*\d+[a-z]?\s*\)\s*$", "", text, flags=re.I).strip()
    text = re.sub(r"\s*\(\s*\d+\s+weeks?\s*\)\s*$", "", text, flags=re.I).strip()
    text = re.sub(
        r"\s+(?:EUR|USD|RUB|GBP|CNY|RMB|HKD|€|\$|£)\s*(?:m|mn|mln|million|bn|billion)?\.?\s*$",
        "",
        text,
        flags=re.I,
    ).strip()
    text = re.sub(r"\s+(?:m|mn|mln|million|bn|billion)\.?\s*$", "", text, flags=re.I).strip()
    return text


def _fiscal_year_from_token(raw: object) -> int | None:
    text = str(raw or "").strip()
    m = re.fullmatch(r"(?i)(?:FY|Fiscal)\s*['’]?\s*(\d{2}|(?:19|20)\d{2})", text)
    if not m:
        return None
    return _two_or_four_digit_year(m.group(1))


def _normalize_short_period_header(
    raw: object,
    *,
    value_kind: str | None = None,
    fiscal_context: FiscalPeriodContext | None = None,
) -> str:
    text = _strip_period_qualifiers(raw)
    if not text:
        return ""
    fiscal_context = fiscal_context or FiscalPeriodContext.empty()

    fiscal_year = _fiscal_year_from_token(text)
    if fiscal_year:
        context_label = fiscal_context.label_for_year(fiscal_year, value_kind=value_kind)
        if context_label:
            return context_label
        return f"31.12.{fiscal_year}" if _kind_is_point(value_kind) else f"FY {fiscal_year}"

    m = re.fullmatch(
        r"(?i)(?:Q\s*([1-4])|([1-4])\s*Q)\s*(?:FY|Fiscal)\s*['’]?\s*(\d{2}|(?:19|20)\d{2})",
        text,
    )
    if m:
        q = int(m.group(1) or m.group(2))
        year = _two_or_four_digit_year(m.group(3))
        if year:
            end = fiscal_context.end_date_for_year(year)
            if end and q == 4:
                return _date_label(end) if _kind_is_point(value_kind) else _calendar_label_for_duration(3, end)
            if end and fiscal_context.has_shifted_years():
                return ""
            return _label_from_quarter(q, year, value_kind=value_kind)

    m = re.fullmatch(r"(?i)Q\s*([1-4])\s*['’]?\s*(\d{2}|(?:19|20)\d{2})", text)
    if m:
        year = _two_or_four_digit_year(m.group(2))
        if year:
            return _label_from_quarter(int(m.group(1)), year, value_kind=value_kind)

    m = re.fullmatch(r"(?i)([1-4])\s*Q\s*['’]?\s*(\d{2}|(?:19|20)\d{2})", text)
    if m:
        year = _two_or_four_digit_year(m.group(2))
        if year:
            return _label_from_quarter(int(m.group(1)), year, value_kind=value_kind)

    m = re.fullmatch(
        r"(?i)(?:quarter\s*([1-4])|([1-4])(?:st|nd|rd|th)\s+quarter)\s*(?:of\s*)?(\d{2}|(?:19|20)\d{2})",
        text,
    )
    if m:
        q = int(m.group(1) or m.group(2))
        year = _two_or_four_digit_year(m.group(3))
        if year:
            return _label_from_quarter(q, year, value_kind=value_kind)

    m = re.fullmatch(r"(?i)([1-9]|1[0-2])\s*[-–]\s*([1-9]|1[0-2])\s*['’]?\s*(\d{2}|(?:19|20)\d{2})", text)
    if m:
        start_month = int(m.group(1))
        end_month = int(m.group(2))
        year = _two_or_four_digit_year(m.group(3))
        if year and 1 <= start_month <= end_month <= 12:
            end = _month_end_date(year, end_month)
            if _kind_is_point(value_kind):
                return _date_label(end)
            return _calendar_label_for_duration(end_month - start_month + 1, end)

    m = re.fullmatch(
        rf"(?i)({_MONTH_NAME_RE})\s*[-–]\s*({_MONTH_NAME_RE})\s*['’]?\s*(\d{{2}}|(?:19|20)\d{{2}})",
        text,
    )
    if m:
        start_month = _month_from_word(m.group(1))
        end_month = _month_from_word(m.group(2))
        year = _two_or_four_digit_year(m.group(3))
        if year and start_month and end_month and start_month <= end_month:
            end = _month_end_date(year, end_month)
            if _kind_is_point(value_kind):
                return _date_label(end)
            return _calendar_label_for_duration(end_month - start_month + 1, end)

    m = re.fullmatch(rf"(?i)({_MONTH_NAME_RE})\s*[-'/]?\s*(\d{{2}}|(?:19|20)\d{{2}})", text)
    if m:
        month = _month_from_word(m.group(1))
        year = _two_or_four_digit_year(m.group(2))
        if month and year:
            return _label_from_month_year(month, year, value_kind=value_kind)

    return ""


def _date_label(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


def _parse_date(raw: str) -> date | None:
    s = str(raw or "").strip()
    if not s:
        return None

    candidates = [s]
    without_parentheticals = re.sub(r"\([^)]*\)", " ", s)
    without_parentheticals = re.sub(r"\s+", " ", without_parentheticals).strip()
    if without_parentheticals and without_parentheticals != s:
        candidates.append(without_parentheticals)

    for candidate in candidates:
        parsed = _parse_date_candidate(candidate)
        if parsed:
            return parsed

    return None


def _parse_date_candidate(s: str) -> date | None:
    s = str(s or "").strip()
    if not s:
        return None

    # Numeric: 05.11.2025 / 5/11/2025 / 2025-11-05.
    m = re.search(r"\b(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b", s)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](20\d{2})\b", s)
    if m:
        d, mo, y = map(int, m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2})\b", s)
    if m:
        d, mo, yy = map(int, m.groups())
        y = _two_or_four_digit_year(yy)
        if y:
            try:
                return date(y, mo, d)
            except ValueError:
                return None

    # RU/EN: 31 декабря 2024 / 30 JUN. 25 / 31st March 2025 / December 31, 2024.
    m = re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAME_RE})\s*,?\s*((?:19|20)\d{{2}}|\d{{2}})\b",
        s,
        re.I,
    )
    if m:
        d = int(m.group(1))
        mo = _month_from_word(m.group(2))
        y = _two_or_four_digit_year(m.group(3))
        if mo and y:
            try:
                return date(y, mo, d)
            except ValueError:
                return None
    m = re.search(
        rf"\b({_MONTH_NAME_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*((?:19|20)\d{{2}}|\d{{2}})\b",
        s,
        re.I,
    )
    if m:
        mo = _month_from_word(m.group(1))
        d = int(m.group(2))
        y = _two_or_four_digit_year(m.group(3))
        if mo and y:
            try:
                return date(y, mo, d)
            except ValueError:
                return None
    return None


def _find_date_mentions(text: str) -> list[tuple[int, date]]:
    matches: list[tuple[int, date]] = []
    patterns = [
        rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH_NAME_RE}\s*,?\s*(?:19|20)\d{{2}}\b",
        rf"\b{_MONTH_NAME_RE}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s*(?:19|20)\d{{2}}\b",
        r"\b\d{1,2}[.\-/]\d{1,2}[.\-/](?:19|20)\d{2}\b",
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.I):
            parsed = _parse_date_candidate(m.group(0))
            if parsed:
                matches.append((m.start(), parsed))
    return sorted(matches, key=lambda item: item[0])


def _years_in_text(text: str) -> list[int]:
    seen: set[int] = set()
    years: list[int] = []
    for m in re.finditer(r"\b((?:19|20)\d{2})\b", text):
        year = int(m.group(1))
        if year not in seen:
            seen.add(year)
            years.append(year)
    return years


def _assign_fiscal_year_dates(mapping: dict[int, date], years: list[int], dates: list[date]) -> None:
    if not years or not dates:
        return
    if len(years) == len(dates):
        for year, end in zip(years, dates):
            mapping[year] = end
        return
    date_by_year = {end.year: end for end in dates}
    for year in years:
        end = date_by_year.get(year)
        if end:
            mapping[year] = end


def _assign_fixed_year_end_dates(mapping: dict[int, date], years: list[int], month: int, day: int) -> None:
    for year in years:
        try:
            mapping.setdefault(year, date(year, month, day))
        except ValueError:
            continue


def _extract_fiscal_year_end_dates(text: str) -> dict[int, date]:
    """Build {fiscal_year: fiscal_year_end_date} from report prose."""
    src = str(text or "").replace("\u00a0", " ")
    src = re.sub(r"\s+", " ", src[:1_500_000]).strip()
    if not src:
        return {}

    mapping: dict[int, date] = {}
    date_pattern = (
        rf"{_MONTH_NAME_RE}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s*(?:19|20)\d{{2}}"
        rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH_NAME_RE}\s*,?\s*(?:19|20)\d{{2}}"
        r"|\d{1,2}[.\-/]\d{1,2}[.\-/](?:19|20)\d{2}"
    )

    fiscal_label_patterns = (
        rf"\bfiscal\s+(?P<year>\d{{2}}|(?:19|20)\d{{2}})\s*\|\s*"
        rf"fiscal\s+year\s+(?:ended|ending)\s+(?P<date>{date_pattern})",
        rf"\bfiscal\s+(?P<year>\d{{2}}|(?:19|20)\d{{2}})\s+means\s+"
        rf"[^.]{{0,120}}?\bfiscal\s+year\s+(?:ended|ending)\s+(?P<date>{date_pattern})",
        rf"\bfiscal\s+(?P<year>\d{{2}}|(?:19|20)\d{{2}})\s+(?:is|was)\s+"
        rf"(?:the\s+)?(?:\d{{2}}\s*[-/]?\s*week\s+)?fiscal\s+year\s+(?:ended|ending)\s+(?P<date>{date_pattern})",
        rf"\bfiscal\s+(?P<year>\d{{2}}|(?:19|20)\d{{2}})\s+(?:ended|ending)\s+(?P<date>{date_pattern})",
        rf"\bfiscal\s+year\s+(?P<year>\d{{2}}|(?:19|20)\d{{2}})\s+(?:ended|ending)\s+(?P<date>{date_pattern})",
    )
    for pattern in fiscal_label_patterns:
        for m in re.finditer(pattern, src, re.I):
            year = _two_or_four_digit_year(m.group("year"))
            end = _parse_date_candidate(m.group("date"))
            if year and end:
                mapping[year] = end

    q4_fiscal_pat = re.compile(
        rf"\b(?:fourth|4(?:th)?|Q4)\s+(?:quarter\s+)?ended\s+(?P<date>{date_pattern})"
        rf"[^.()]{{0,100}}?\(\s*\\?[\"'“”]?\s*Q4\s*FY\s*(?P<year>\d{{2}}|(?:19|20)\d{{2}})",
        re.I,
    )
    for m in q4_fiscal_pat.finditer(src):
        year = _two_or_four_digit_year(m.group("year"))
        end = _parse_date_candidate(m.group("date"))
        if year and end:
            mapping.setdefault(year, end)

    plural_years_ended_pat = re.compile(rf"\byears\s+ended\s+(?P<date>{date_pattern})", re.I)
    for m in plural_years_ended_pat.finditer(src):
        end = _parse_date_candidate(m.group("date"))
        if end:
            mapping.setdefault(end.year, end)

    references_pat = re.compile(
        r"\breferences?\s+to\s+(?P<years>[^.]{0,120}?)\s+relate\s+to\s+[^.]{0,220}?\bended\s+(?P<dates>[^.]{0,240})",
        re.I,
    )
    for m in references_pat.finditer(src):
        years = _years_in_text(m.group("years"))
        dates = [item[1] for item in _find_date_mentions(m.group("dates"))]
        _assign_fiscal_year_dates(mapping, years, dates)

    fiscal_years_pat = re.compile(r"\bfiscal\s+years?\s+ended\s+(?P<dates>[^.]{0,260})", re.I)
    for m in fiscal_years_pat.finditer(src):
        dates = [item[1] for item in _find_date_mentions(m.group("dates"))]
        for end in dates:
            mapping.setdefault(end.year, end)

    cover_pat = re.compile(rf"\bfor\s+the\s+fiscal\s+year\s+ended\s+(?P<date>{date_pattern})", re.I)
    for m in cover_pat.finditer(src):
        end = _parse_date_candidate(m.group("date"))
        if end:
            mapping.setdefault(end.year, end)
            if "52/53" not in src[:20_000] and "52- or 53-week" not in src[:20_000].lower():
                _assign_fixed_year_end_dates(mapping, [end.year - i for i in range(1, 6)], end.month, end.day)

    month_day_years_pat = re.compile(
        rf"\byears\s+ended\s+(?P<month>{_MONTH_NAME_RE})\s+(?P<day>\d{{1,2}})\b"
        rf"(?!\s*,?\s*(?:19|20)\d{{2}})(?P<tail>[^.]{{0,420}})",
        re.I,
    )
    max_context_year = max(mapping) if mapping else None
    for m in month_day_years_pat.finditer(src):
        month = _month_from_word(m.group("month"))
        day = int(m.group("day"))
        if not month:
            continue
        years = _years_in_text(m.group("tail"))[:8]
        if max_context_year:
            years = [year for year in years if max_context_year - 6 <= year <= max_context_year]
        _assign_fixed_year_end_dates(mapping, years, month, day)

    return mapping


def _calendar_label_for_duration(months: int, end: date) -> str:
    """Return canonical label for a duration ending on `end`.

    Calendar-aligned periods get FY/Q/6M/9M labels. Shifted fiscal periods keep
    their exact end date (`3M 28.02.2026`, `12M 31.01.2025`) so they are not
    confused with calendar quarters or years.
    """
    if months == 12 and end.month == 12 and end.day == 31:
        return f"FY {end.year}"
    if months == 9 and end.month == 9 and end.day == 30:
        return f"9M {end.year}"
    if months == 6 and end.month == 6 and end.day == 30:
        return f"6M {end.year}"
    if months == 3:
        quarter_ends = {(3, 31): 1, (6, 30): 2, (9, 30): 3, (12, 31): 4}
        q = quarter_ends.get((end.month, end.day))
        if q:
            return f"Q{q} {end.year}"
    return f"{months}M {_date_label(end)}"


def _duration_components_from_label(label: object) -> tuple[int, date] | None:
    period = normalize_period_label(label, value_kind=DURATION_KIND)
    if m := re.fullmatch(r"FY (20\d{2})", period):
        return 12, date(int(m.group(1)), 12, 31)
    if m := re.fullmatch(r"9M (20\d{2})", period):
        return 9, date(int(m.group(1)), 9, 30)
    if m := re.fullmatch(r"6M (20\d{2})", period):
        return 6, date(int(m.group(1)), 6, 30)
    if m := re.fullmatch(r"Q([1-4]) (20\d{2})", period):
        quarter = int(m.group(1))
        year = int(m.group(2))
        month = quarter * 3
        return 3, date(year, month, calendar.monthrange(year, month)[1])
    if m := re.fullmatch(r"(\d{1,2})M (\d{1,2})[.](\d{1,2})[.](20\d{2})", period):
        months, day, month, year = map(int, m.groups())
        try:
            return months, date(year, month, day)
        except ValueError:
            return None
    return None


def _indian_fiscal_start_year_for_end(end: date) -> int:
    """Indian FY label is named by the year it starts in: FY 2024 ends 31.03.2025."""
    return end.year - 1 if end.month <= 3 else end.year


def _indian_fiscal_year_range_start(raw: object) -> int | None:
    text = str(raw or "").replace("\u00a0", " ").strip()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    m = re.search(
        r"(?i)\b(?:FY|F\.Y\.|financial\s+year|fiscal\s+year)\s*['’]?"
        r"(?P<start>\d{2}|(?:19|20)\d{2})\s*[-–/]\s*['’]?"
        r"(?P<end>\d{2}|(?:19|20)\d{2})\b",
        text,
    )
    if not m:
        return None
    start = _two_or_four_digit_year(m.group("start"))
    end = _two_or_four_digit_year(m.group("end"))
    if start is None or end is None:
        return None
    if end < start:
        end += 100
    # Only treat adjacent-year ranges as fiscal-year labels.
    return start if end in {start, start + 1} else None


def _indian_fiscal_single_year_start(raw: object) -> int | None:
    text = str(raw or "").replace("\u00a0", " ").strip()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    m = re.fullmatch(
        r"(?i)(?P<prefix>FY|F\.Y\.|financial\s+year|fiscal\s+year)(?P<space>\s*)['’]?"
        r"(?P<year>\d{2}|(?:19|20)\d{2})",
        text,
    )
    if not m:
        return None
    year = _two_or_four_digit_year(m.group("year"))
    if year is None:
        return None
    prefix = m.group("prefix").lower()
    # Issuers normally use compact FY25/FY2025 and "financial year 2025"
    # for the year *ending* in March 2025. The canonical public label below
    # uses the Indian FY start year instead. A spaced `FY 2024` is the canonical
    # start-year form and therefore remains unchanged.
    if len(m.group("year")) == 2 or not m.group("space") or prefix != "fy":
        return year - 1
    return year


def _indian_fiscal_qualified_quarter(
    raw: object,
    *,
    value_kind: str | None = None,
) -> str:
    """Normalize issuer shorthand such as Q1 FY26 using FY end-year semantics."""
    text = str(raw or "").replace("\u00a0", " ").strip()
    text = re.sub(r"\s+", " ", text)
    m = re.fullmatch(
        r"(?i)(?:Q\s*([1-4])|([1-4])\s*Q)\s*"
        r"(?:FY|F\.Y\.|financial\s+year|fiscal\s+year)\s*['’]?"
        r"(\d{2}|(?:19|20)\d{2})",
        text,
    )
    if not m:
        return ""
    quarter = int(m.group(1) or m.group(2))
    end_year = _two_or_four_digit_year(m.group(3))
    if end_year is None:
        return ""
    start_year = end_year - 1
    if _kind_is_point(value_kind):
        end = {
            1: date(start_year, 6, 30),
            2: date(start_year, 9, 30),
            3: date(start_year, 12, 31),
            4: date(end_year, 3, 31),
        }[quarter]
        return _date_label(end)
    return f"Q{quarter} {start_year}"


def _normalized_indian_fiscal_label(
    raw: object,
    *,
    value_kind: str | None = None,
) -> str:
    """Preserve canonical start-year labels when they pass through twice."""
    text = str(raw or "").replace("\u00a0", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if m := re.fullmatch(r"(?i)FY\s+(20\d{2})", text):
        start_year = int(m.group(1))
        return (
            _date_label(date(start_year + 1, 3, 31))
            if _kind_is_point(value_kind)
            else f"FY {start_year}"
        )
    if m := re.fullmatch(r"(?i)Q\s*([1-4])\s+(20\d{2})", text):
        quarter = int(m.group(1))
        start_year = int(m.group(2))
        if _kind_is_point(value_kind):
            end = {
                1: date(start_year, 6, 30),
                2: date(start_year, 9, 30),
                3: date(start_year, 12, 31),
                4: date(start_year + 1, 3, 31),
            }[quarter]
            return _date_label(end)
        return f"Q{quarter} {start_year}"
    if m := re.fullmatch(r"(?i)([69])\s*M\s+(20\d{2})", text):
        months = int(m.group(1))
        start_year = int(m.group(2))
        if _kind_is_point(value_kind):
            month, day = {6: (9, 30), 9: (12, 31)}[months]
            return _date_label(date(start_year, month, day))
        return f"{months}M {start_year}"
    return ""


def _indian_fiscal_label_from_duration(months: int, end: date) -> str:
    start_year = _indian_fiscal_start_year_for_end(end)
    if months == 12 and (end.month, end.day) == (3, 31):
        return f"FY {start_year}"
    if months == 9 and (end.month, end.day) == (12, 31):
        return f"9M {start_year}"
    if months == 6 and (end.month, end.day) == (9, 30):
        return f"6M {start_year}"
    if months == 3:
        fiscal_quarter = {
            (6, 30): 1,
            (9, 30): 2,
            (12, 31): 3,
            (3, 31): 4,
        }.get((end.month, end.day))
        if fiscal_quarter:
            return f"Q{fiscal_quarter} {start_year}"
    return ""


def _indian_fiscal_label_from_bare_duration_date(end: date) -> str:
    start_year = _indian_fiscal_start_year_for_end(end)
    if (end.month, end.day) == (3, 31):
        return f"FY {start_year}"
    if (end.month, end.day) == (6, 30):
        return f"Q1 {start_year}"
    if (end.month, end.day) == (9, 30):
        return f"6M {start_year}"
    if (end.month, end.day) == (12, 31):
        return f"9M {start_year}"
    return f"12M {_date_label(end)}"


def _indian_fiscal_repair_mixed_year_header(raw: object) -> str:
    text = str(raw or "").replace("\u00a0", " ").strip()
    if not text or not re.search(r"\byears?\s+ended\b", text, re.I):
        return ""
    end = _parse_date(text)
    if end is None:
        return ""
    if (end.month, end.day) in {(6, 30), (9, 30), (12, 31), (3, 31)}:
        return _indian_fiscal_label_from_bare_duration_date(end)
    return ""


def normalize_indian_fiscal_period_label(
    label: object,
    *,
    value_kind: str | None = None,
    already_normalized: bool = False,
) -> str:
    """Convert duration labels to Indian Apr-Mar FY labels.

    Indian reports usually treat April-March as the financial year and name it
    by the starting year: FY 2024 / FY 2024-25 ends on 31.03.2025. Point
    metrics remain exact dates unless the raw header itself is a fiscal-year
    label, in which case it points to that Indian year-end date. Set
    ``already_normalized`` only when an internal start-year label is reprocessed.
    """
    if already_normalized:
        normalized_label = _normalized_indian_fiscal_label(label, value_kind=value_kind)
        if normalized_label:
            return normalized_label

    range_start_year = _indian_fiscal_year_range_start(label)
    if range_start_year is not None:
        if _kind_is_point(value_kind):
            return _date_label(date(range_start_year + 1, 3, 31))
        return f"FY {range_start_year}"

    qualified_quarter = _indian_fiscal_qualified_quarter(label, value_kind=value_kind)
    if qualified_quarter:
        return qualified_quarter

    single_start_year = _indian_fiscal_single_year_start(label)
    if single_start_year is not None:
        if _kind_is_point(value_kind):
            return _date_label(date(single_start_year + 1, 3, 31))
        return f"FY {single_start_year}"

    if _kind_is_duration(value_kind):
        repaired_year_header = _indian_fiscal_repair_mixed_year_header(label)
        if repaired_year_header:
            return repaired_year_header

        raw_date = _parse_date(str(label or ""))
        if raw_date is not None and not re.search(
            r"\b(quarter|months?|year|half|semester|FY|F\.Y\.|financial|fiscal|квартал|год|мес)",
            str(label or ""),
            re.I,
        ):
            return _indian_fiscal_label_from_bare_duration_date(raw_date)

    period = normalize_period_label(label, value_kind=value_kind)
    if not period or _kind_is_point(value_kind):
        return period
    components = _duration_components_from_label(period)
    if not components:
        return period
    months, end = components
    return _indian_fiscal_label_from_duration(months, end) or period


def _duration_label_from_date(end: date) -> str:
    """Infer a duration label from a bare end date for duration metrics.

    This is a fallback for LLM period-header drift such as returning
    "as of February 28, 2022" for a P&L metric. Explicit "three/six/nine/twelve
    months ended" text still wins when present.
    """
    if end.month == 12 and end.day == 31:
        return f"FY {end.year}"
    if end.month == 9 and end.day == 30:
        return f"9M {end.year}"
    if end.month == 6 and end.day == 30:
        return f"6M {end.year}"
    if end.month == 3 and end.day == 31:
        return f"Q1 {end.year}"
    return f"12M {_date_label(end)}"


def _point_label_from_period(months: int, end: date) -> str:
    """Coerce a duration period into its end date for point-in-time metrics."""
    return _date_label(end)


def _semester_end_date(raw: str, parsed_date: date | None = None) -> date | None:
    """Infer the end date for semester / half-year period headers."""
    s = str(raw or "").strip()
    sl = s.lower()

    year_match = re.search(r"\b(20\d{2})\b", s)
    year = int(year_match.group(1)) if year_match else None

    first_patterns = (
        r"\b(?:1st|first|1)\s+(?:semester|half(?:[-\s]?year)?)\b",
        r"\b(?:semester|half(?:[-\s]?year)?)\s+(?:1st|first|1)\b",
        r"\b(?:h1|1h)\b",
        r"\b(?:i)\s+semester\b",
        r"\bsemester\s+(?:i)\b",
        r"\b(?:1\s*[-–]?\s*е|перв(?:ое|ый|ого)?)\s+полугод",
    )
    if year and any(re.search(pattern, sl, re.I) for pattern in first_patterns):
        return date(year, 6, 30)

    second_patterns = (
        r"\b(?:2nd|second|2)\s+(?:semester|half(?:[-\s]?year)?)\b",
        r"\b(?:semester|half(?:[-\s]?year)?)\s+(?:2nd|second|2)\b",
        r"\b(?:h2|2h)\b",
        r"\b(?:ii)\s+semester\b",
        r"\bsemester\s+(?:ii)\b",
        r"\b(?:2\s*[-–]?\s*е|втор(?:ое|ой|ого)?)\s+полугод",
    )
    if year and any(re.search(pattern, sl, re.I) for pattern in second_patterns):
        return date(year, 12, 31)

    has_semester_word = bool(re.search(r"\b(?:semester|half(?:[-\s]?year)?|полугод)\b", sl))
    if parsed_date and has_semester_word:
        return parsed_date

    if year and re.search(r"\b(?:half[-\s]?year|полугод)\b", sl):
        return date(year, 6, 30)

    return None


def normalize_period_label(
    raw: object,
    *,
    value_kind: str | None = None,
    fiscal_context: FiscalPeriodContext | None = None,
) -> str:
    """Normalize a raw period label or table header to the public period format."""
    s = str(raw or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s.replace("\u00a0", " ")).strip()
    fiscal_context = fiscal_context or FiscalPeriodContext.empty()

    # Already canonical enough.
    m = re.fullmatch(r"(\d{1,2})[.](\d{1,2})[.](20\d{2})", s)
    if m:
        d, mo, y = map(int, m.groups())
        try:
            parsed_date = date(y, mo, d)
        except ValueError:
            return f"{d}.{mo:02d}.{y}"
        if _kind_is_duration(value_kind):
            return _duration_label_from_date(parsed_date)
        return _date_label(parsed_date)
    m = re.fullmatch(r"(?i)FY\s*(20\d{2})", s)
    if m:
        year = int(m.group(1))
        context_label = fiscal_context.label_for_year(year, value_kind=value_kind)
        if context_label:
            return context_label
        if _kind_is_point(value_kind):
            return f"31.12.{year}"
        return f"FY {year}"
    m = re.fullmatch(r"(?i)Q\s*([1-4])\s*(20\d{2})", s)
    if m:
        q = int(m.group(1))
        year = int(m.group(2))
        if _kind_is_point(value_kind):
            end_month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[q]
            return _date_label(date(year, *end_month_day))
        return f"Q{q} {year}"
    m = re.fullmatch(r"(?i)([369])\s*M\s*(20\d{2})", s)
    if m:
        months = int(m.group(1))
        year = int(m.group(2))
        if _kind_is_point(value_kind):
            end_day = 31 if months == 3 else 30
            return _date_label(date(year, months, end_day))
        return f"{months}M {year}"
    m = re.fullmatch(r"(?i)(\d{1,2})\s*M\s*(\d{1,2}[.]\d{1,2}[.]20\d{2})", s)
    if m:
        months = int(m.group(1))
        d = _parse_date(m.group(2))
        if d:
            if _kind_is_point(value_kind):
                return _point_label_from_period(months, d)
            return _calendar_label_for_duration(months, d)
        return f"{months}M {normalize_period_label(m.group(2), value_kind=value_kind, fiscal_context=fiscal_context)}"

    short_header_label = _normalize_short_period_header(
        s,
        value_kind=value_kind,
        fiscal_context=fiscal_context,
    )
    if short_header_label:
        return short_header_label
    stripped_for_year = _strip_period_qualifiers(s)
    if stripped_for_year != s and re.fullmatch(r"20\d{2}", stripped_for_year):
        year = int(stripped_for_year)
        context_label = fiscal_context.label_for_year(year, value_kind=value_kind)
        if context_label:
            return context_label
        return f"31.12.{year}" if _kind_is_point(value_kind) else f"FY {year}"

    # Plain year means a calendar-year period value; point-in-time dates should be explicit.
    if re.fullmatch(r"20\d{2}", s):
        context_label = fiscal_context.label_for_year(int(s), value_kind=value_kind)
        if context_label:
            return context_label
        return f"31.12.{s}" if _kind_is_point(value_kind) else f"FY {s}"
    m = re.fullmatch(r"(20\d{2})\s*(?:г\.?|год(?:а)?)", s, re.I)
    if m:
        year = int(m.group(1))
        context_label = fiscal_context.label_for_year(year, value_kind=value_kind)
        if context_label:
            return context_label
        return f"31.12.{year}" if _kind_is_point(value_kind) else f"FY {year}"

    sl = s.lower()
    d = _parse_date(s)

    semester_end = _semester_end_date(s, d)
    if semester_end:
        if _kind_is_point(value_kind):
            return _point_label_from_period(6, semester_end)
        return _calendar_label_for_duration(6, semester_end)

    # Explicit months-duration headers.
    m = re.search(
        rf"\b(three|six|nine|twelve|3|6|9|12|три|тр[её]х|шесть|шести|"
        rf"девять|девяти|двенадцать|двенадцати)\s+"
        rf"(?:months?|мес(?:яцев|яца|\.?)?)\b.*?({_MONTH_NAME_RE}|\d{{1,2}}[.\-/])",
        sl,
        re.I,
    )
    if m and d:
        token = m.group(1).lower()
        months = int(token) if token.isdigit() else _WORD_TO_MONTHS.get(token, 0)
        if months:
            if _kind_is_point(value_kind):
                return _point_label_from_period(months, d)
            return _calendar_label_for_duration(months, d)

    # Headers like "Quarter Ended March 29, 2025" mean a three-month period.
    # If the end date is not a calendar quarter end, keep the exact end date.
    if d and re.search(r"\b(quarter|квартал)\b", sl):
        if _kind_is_point(value_kind):
            return _point_label_from_period(3, d)
        return _calendar_label_for_duration(3, d)

    # Point-in-time balance-sheet headers must stay dates even when the date
    # itself contains "года" / "year". Example: "по состоянию на 31 декабря
    # 2013 года" is 31.12.2013, not FY 2013.
    if d and re.search(_POINT_IN_TIME_RE, sl, re.I):
        if _kind_is_duration(value_kind):
            return _duration_label_from_date(d)
        return _date_label(d)

    # "Год, закончившийся 31 декабря 2024" / "year(s) ended December 31, 2024".
    if d and re.search(r"\b(years?|год|года|12\s*мес|twelve\s+months)\b", sl):
        if _kind_is_point(value_kind):
            return _point_label_from_period(12, d)
        return _calendar_label_for_duration(12, d)
    if d:
        if _kind_is_duration(value_kind):
            return _duration_label_from_date(d)
        return _date_label(d)

    # Headers like "2024 год" / "2024 fiscal year".
    m = re.search(r"\b(20\d{2})\b", s)
    if m:
        year = int(m.group(1))
        if re.search(r"\b(q|quarter|квартал)\s*([1-4])", sl):
            q = re.search(r"\b(?:q|quarter|квартал)\s*([1-4])", sl).group(1)
            return f"Q{q} {year}"
        if re.search(
            r"\b(6\s*мес|six\s+months|шесть\s+месяц|шести\s+месяц|half\s+year|полугод)",
            sl,
        ):
            return f"6M {year}"
        if re.search(r"\b(9\s*мес|nine\s+months|девять\s+месяц|девяти\s+месяц)", sl):
            return f"9M {year}"
        if re.search(r"\b(year|год|года|г\.?|fy)\b", sl):
            context_label = fiscal_context.label_for_year(year, value_kind=value_kind)
            if context_label:
                return context_label
            return f"31.12.{year}" if _kind_is_point(value_kind) else f"FY {year}"

    return s


def normalize_period_for_enum(
    raw: object,
    enum: object | None,
    *,
    fiscal_context: FiscalPeriodContext | None = None,
) -> str:
    """Normalize `raw` using the period-kind contract for a metric enum."""
    return normalize_period_label(
        raw,
        value_kind=period_kind_for_enum(enum),
        fiscal_context=fiscal_context,
    )


def _duration_match_to_label(months_raw: str, date_raw: str) -> str:
    token = months_raw.lower()
    months = int(token) if token.isdigit() else _WORD_TO_MONTHS.get(token, 0)
    d = _parse_date(date_raw)
    if not months or not d:
        return ""
    return _calendar_label_for_duration(months, d)


def detect_period_candidates(
    text: str,
    *,
    max_periods: int = 14,
    fiscal_context: FiscalPeriodContext | None = None,
) -> list[str]:
    """Extract likely reporting periods from document/table headers."""
    src = (text or "")[:250_000].replace("\u00a0", " ")
    fiscal_context = fiscal_context or FiscalPeriodContext.from_text(src)
    found: dict[str, tuple] = {}

    def add(label: str) -> None:
        norm = normalize_period_label(label, fiscal_context=fiscal_context)
        if norm:
            found[norm] = period_sort_key(norm)

    for year in sorted(fiscal_context.year_end_dates, reverse=True):
        add(str(year))

    date_year_sep = r"\s*,?\s*(?:\([^)]*\)\s*)*"
    year_token = r"(?:(?:19|20)\d{2}|\d{2})"
    date_pat1 = rf"\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH_NAME_RE}{date_year_sep}{year_token}"
    date_pat2 = rf"{_MONTH_NAME_RE}\s+\d{{1,2}}(?:st|nd|rd|th)?{date_year_sep}{year_token}"
    date_pat3 = rf"\d{{1,2}}[.\-/]\d{{1,2}}[.\-/]{year_token}"
    date_any = rf"(?:{date_pat1}|{date_pat2}|{date_pat3})"

    duration_token = (
        r"three|six|nine|twelve|3|6|9|12|три|тр[её]х|шесть|шести|"
        r"девять|девяти|двенадцать|двенадцати"
    )
    combined_duration_pat = (
        rf"\b({duration_token})\s+(?:and|и)\s+({duration_token})\s+"
        rf"(?:months?|мес(?:яцев|яца|\.?)?)\b[^\n|]{{0,80}}?({date_any})"
    )
    for m in re.finditer(combined_duration_pat, src, re.I):
        for token in (m.group(1), m.group(2)):
            label = _duration_match_to_label(token, m.group(3))
            if label:
                found[label] = period_sort_key(label)

    duration_pat = (
        rf"\b({duration_token})\s+"
        rf"(?:months?|мес(?:яцев|яца|\.?)?)\b[^\n|]{{0,80}}?({date_any})"
    )
    for m in re.finditer(duration_pat, src, re.I):
        label = _duration_match_to_label(m.group(1), m.group(2))
        if label:
            found[label] = period_sort_key(label)

    quarter_date_pat = rf"\b(?:quarter|квартал)\b[^\n|]{{0,80}}?({date_any})"
    for m in re.finditer(quarter_date_pat, src, re.I):
        d = _parse_date(m.group(1))
        if d:
            label = _calendar_label_for_duration(3, d)
            found[label] = period_sort_key(label)

    semester_pat = (
        r"\b(?:1st|first|2nd|second|1|2|h1|1h|h2|2h)\s+"
        r"(?:semester|half(?:[-\s]?year)?)\b[^\n|]{0,40}?\b20\d{2}\b|"
        r"\b(?:semester|half(?:[-\s]?year)?)\s+"
        r"(?:1st|first|2nd|second|1|2)\b[^\n|]{0,40}?\b20\d{2}\b|"
        r"\b(?:h1|1h|h2|2h)\s*(?:of\s*)?20\d{2}\b"
    )
    for m in re.finditer(semester_pat, src, re.I):
        add(m.group(0))

    semester_date_pat = (
        rf"\b(?:1st|first|2nd|second|1|2)?\s*"
        rf"(?:semester|half(?:[-\s]?year)?)\b[^\n|]{{0,80}}?({date_any})"
    )
    for m in re.finditer(semester_date_pat, src, re.I):
        add(m.group(0))

    year_pat = rf"\b(?<!half\s)(?:years?|год|12\s*мес|twelve\s+months)\b[^\n|]{{0,80}}?({date_any})"
    for m in re.finditer(year_pat, src, re.I):
        add(m.group(0))

    point_pat = rf"\b(?:as of|по состоянию на|на дату)\b[^\n|]{{0,40}}?({date_any})"
    for m in re.finditer(point_pat, src, re.I):
        d = _parse_date(m.group(1))
        if d:
            found[_date_label(d)] = period_sort_key(_date_label(d))

    for m in re.finditer(r"\b(?:FY|финансовый год|год)\s*(20\d{2})\b", src, re.I):
        add(f"FY {m.group(1)}")
    for m in re.finditer(r"\bQ\s*([1-4])\s*(20\d{2})\b", src, re.I):
        add(f"Q{m.group(1)} {m.group(2)}")
    for m in re.finditer(r"\b([69])\s*M\s*(20\d{2})\b", src, re.I):
        add(f"{m.group(1)}M {m.group(2)}")

    return [p for p, _ in sorted(found.items(), key=lambda kv: kv[1], reverse=True)[:max_periods]]


def previous_period_label(label: str) -> str:
    """Best-effort previous comparable period for legacy current/previous fields."""
    p = normalize_period_label(label)
    m = re.fullmatch(r"FY (20\d{2})", p)
    if m:
        return f"FY {int(m.group(1)) - 1}"
    m = re.fullmatch(r"Q([1-4]) (20\d{2})", p)
    if m:
        return f"Q{m.group(1)} {int(m.group(2)) - 1}"
    m = re.fullmatch(r"([69])M (20\d{2})", p)
    if m:
        return f"{m.group(1)}M {int(m.group(2)) - 1}"
    m = re.fullmatch(r"(\d{1,2})M (\d{1,2})[.](\d{1,2})[.](20\d{2})", p)
    if m:
        months, d, mo, y = m.groups()
        return f"{int(months)}M {int(d):02d}.{int(mo):02d}.{int(y) - 1}"
    m = re.fullmatch(r"(\d{1,2})[.](\d{1,2})[.](20\d{2})", p)
    if m:
        d, mo, y = m.groups()
        return f"{int(d):02d}.{int(mo):02d}.{int(y) - 1}"
    return "Предыдущий"


def period_sort_key(label: object) -> tuple:
    """Sort key where later periods compare greater."""
    p = normalize_period_label(label)
    m = re.fullmatch(r"FY (20\d{2})", p)
    if m:
        return (int(m.group(1)), 12, 31, 4, 12)
    m = re.fullmatch(r"Q([1-4]) (20\d{2})", p)
    if m:
        q = int(m.group(1))
        return (int(m.group(2)), q * 3, 31, 3, q)
    m = re.fullmatch(r"([69])M (20\d{2})", p)
    if m:
        months = int(m.group(1))
        return (int(m.group(2)), months, 30, 2, months)
    m = re.fullmatch(r"(\d{1,2})M (\d{1,2})[.](\d{1,2})[.](20\d{2})", p)
    if m:
        months, d, mo, y = map(int, m.groups())
        return (y, mo, d, 1, months)
    m = re.fullmatch(r"(\d{1,2})[.](\d{1,2})[.](20\d{2})", p)
    if m:
        d, mo, y = map(int, m.groups())
        return (y, mo, d, 0, 0)
    m = re.search(r"(20\d{2})", p)
    return (int(m.group(1)) if m else 0, 0, 0, 0, 0)


def sort_periods(periods: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for p in periods:
        n = normalize_period_label(p)
        if n:
            seen[n] = None
    return sorted(seen.keys(), key=period_sort_key, reverse=True)
