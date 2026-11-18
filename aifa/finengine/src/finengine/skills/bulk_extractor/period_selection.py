"""Выбор периодов прогона и их сортировка.

Сюда же переехали `_point_period_key` и `_period_end_key`: они периодные по
смыслу, а лежали в зоне вычислителя формул. Оставь их там — `period_selection`
ссылался бы вперёд по порядку зависимостей, и разбиение уткнулось бы в круг.

Здесь же живёт отсечение незакрытого периода (#255): отчёт не может отчитываться
за период, который на его дату ещё не закрыт.
"""
from __future__ import annotations

import calendar
import math
import re
from datetime import date, timedelta
from typing import Mapping

from .value_parsing import _parse_verbatim_value
from finengine.skills.periods import (
    DURATION_KIND,
    POINT_KIND,
    FiscalPeriodContext,
    detect_period_candidates,
    normalize_indian_fiscal_period_label,
    normalize_period_label,
    period_kind_for_enum,
    previous_period_label,
    sort_periods,
)


# Первые числа кварталов. Ими датируют формы РСБУ («по состоянию на 1 октября»),
# и `_is_preferred_report_period` их принимает — правильно. Но датой САМОГО
# отчёта такое число быть не может: это начало ещё не закрытого периода (#259).
_PERIOD_START_DAYS: set[tuple[int, int]] = {(1, 1), (4, 1), (7, 1), (10, 1)}

# Последние числа кварталов — на них датируют БАЛАНС. Дата подписания, дата
# события и дата погашения такой формы почти никогда не имеют, и в
# `_max_report_anchor_year` это единственное, чем голая дата отличается от даты
# отчётности, когда прочих свидетельств за её год в документе нет.
_PERIOD_END_DAYS: set[tuple[int, int]] = {(3, 31), (6, 30), (9, 30), (12, 31)}


# Сколько текста считать ТИТУЛЬНОЙ областью. Полторы тысячи символов — это
# обложка и, самое большее, начало оглавления: там стоит название отчёта и его
# период, и почти ничего больше. Брать шире опасно — уже на шести тысячах у
# Nordea в область попадает «6M 2026» из сравнительной справки.
_ТИТУЛ = 1500


def _prefer_period_named_on_the_cover(periods: list[str], pdf_text: str,
                                      fiscal_context: "FiscalPeriodContext | None" = None,
                                      ) -> list[str]:
    """Поставить первым период, названный на обложке, если он там ОДИН.

    ЗАЧЕМ (прогон KBC, 26.08.2026). В отчёте за 1Q2026 есть раздел финансового
    календаря с расписанием будущих публикаций:

        2Q2026 earnings release: 6 August 2026
        3Q2026 earnings release: 12 November 2026

    Это законные на вид кандидаты, и `Q2 2026` побеждал как самый поздний из
    закрывшихся. Отличить расписание от отчёта по словам («earnings release»)
    нельзя — такое правило сломается на первой чужой формулировке.

    А вот структурное свидетельство есть, и оно надёжное: СВОЙ период отчёт
    называет на обложке. У KBC первые полторы тысячи символов дают ровно один
    кандидат — `Q1 2026`, и это верный ответ.

    🔴 ПРАВИЛО НАРОЧНО УЗКОЕ, И ЭТО ЕГО ГЛАВНОЕ СВОЙСТВО:

      * период на обложке должен быть РОВНО ОДИН. Несколько — свидетельства
        нет, гадать не станем;
      * он должен УЖЕ БЫТЬ среди кандидатов. Обложка помогает выбрать из
        найденного, а не добавляет ненайденное;
      * ничего не нашлось — не трогаем порядок вовсе. Так ведут себя и БСПб, и
        Nordea: у них титул периода не называет, и поведение остаётся прежним.
    """
    if len(periods) < 2:
        return periods
    с_обложки = detect_period_candidates((pdf_text or "")[:_ТИТУЛ],
                                         fiscal_context=fiscal_context)
    if len(с_обложки) != 1:
        return periods
    названный = с_обложки[0]
    if названный not in periods:
        return periods
    return [названный, *[p for p in periods if p != названный]]


def _drop_periods_ending_after_today(periods: list[str]) -> list[str]:
    """Убрать периоды, которые НЕ ЗАКОНЧИЛИСЬ К СЕГОДНЯШНЕМУ ДНЮ.

    ЗАЧЕМ (прогон KBC, 26.08.2026). У отчёта за 1Q2026 детектор нашёл среди
    кандидатов `Q3 2026` и `Q2 2026` — и выбрал первый, как самый поздний.
    Взялись они из раздела финансового календаря:

        3Q2026 earnings release: 12 November 2026

    Это расписание будущих публикаций, а не периоды отчёта. Отличить их по
    контексту значило бы завести правило про слова («earnings release», «отчёт
    выйдет»), а такие правила ломаются на первой же чужой формулировке.

    🔴 ПОЧЕМУ ЗДЕСЬ ЗАКОННО СПРАШИВАТЬ СЕГОДНЯШНЮЮ ДАТУ, хотя весь остальной
    модуль решает по документу. Это не суждение о смысле, а арифметика: мы
    ЧИТАЕМ документ сейчас, и период, который на данный момент ещё не
    закончился, отчётным быть не может — данных за него не существует ни у
    кого. Соседний фильтр (`_drop_periods_that_have_not_closed`) выражает ту же
    мысль, но опирается на дату ИЗ ТЕКСТА; когда точечных дат в тексте нет
    вовсе (как здесь), у него нет якоря и он пропускает всё.

    ⚠️ ГРАНИЦА МЯГКАЯ — по КОНЦУ периода, а не по началу: квартал, кончившийся
    вчера, законен. И фильтр не опустошает список: если под запрет попало всё,
    отдаём вход — остаться без периодов хуже, чем оставить лишний.
    """
    from datetime import date as _date  # noqa: PLC0415

    сегодня = _date.today()
    предел = (сегодня.year, сегодня.month, сегодня.day)
    out = [p for p in periods if (_period_end_key(p) or предел) <= предел]
    return out or periods


def _drop_periods_that_have_not_closed(periods: list[str]) -> list[str]:
    """Убрать периоды, которые на дату отчёта ЕЩЁ НЕ ЗАКОНЧИЛИСЬ (#255).

    ЗАЧЕМ. У БСПб (полугодовой отчёт) движок выбрал периодом прогона `FY 2025`
    вместо полугодия. Сверка трёх промежуточных отчётов стенда показала, чем он
    отличается от двух остальных, — детектор нашёл в тексте ГОДОВУЮ форму:

        Ак Барс      Q2 2025, 6M 2025, 30.06.2025 …   (FY 2025 нет)
        Детский мир  6M 2023, 30.06.2023 …            (FY 2023 нет)
        БСПб         FY 2025, Q2 2025, 6M 2025 …      ← вот он

    `FY 2025` кончается 31.12.2025, то есть ПОЗЖЕ всех прочих, сортировка по
    дате окончания ставит его первым, и он побеждает. А фильтр правдоподобия
    работал по ГОДУ: у `FY 2025` год 2025, как у всего отчёта, — он проходил.

    ПРАВИЛО. Отчёт не может отчитываться за период, который на его дату ещё не
    закрыт. Дату отчёта берём по САМОЙ ПОЗДНЕЙ КАЛЕНДАРНОЙ ДАТЕ среди
    распознанных: даты печатают в шапках форм («30 июня 2025 г.»), и это
    единственная точка отсчёта в тексте, не требующая суждения о смысле.

    ⚠️ ЭТО НЕ РЕШЕНИЕ ПО СМЫСЛУ, а арифметика календаря: 31.12 позже 30.06.
    Выбирать, «какой период главный», по частоте упоминаний было бы решением по
    смыслу, и его место рядом с классификатором, а не в счётчике (граница
    «код возит и считает, модель решает», 09.08).

    Нет ни одной календарной даты — не фильтруем: точки отсчёта нет, а
    выдумывать её значило бы решать за документ.

    🔴 ЯКОРЬ НЕ СТРОИТСЯ ПО ДАТЕ НАЧАЛА ПЕРИОДА (#259). Ак Барс, отчётность на
    30.06.2025, в тексте есть фраза о режиме раскрытия:

        «…заканчивая отчетностью по состоянию на 1 октября 2025 года»

    Это оговорка про Постановление № 1490, а не дата этого отчёта. Но
    `01.10.2025` — законный кандидат (так датируют формы РСБУ), он оказывался
    САМОЙ ПОЗДНЕЙ датой, становился собственным якорем и проходил фильтр
    насквозь: период прогона уезжал на `9M 2025`. Начало периода по построению
    принадлежит периоду ЕЩЁ НЕ ЗАКРЫТОМУ, поэтому датой отчёта служить не
    может — отчётность всегда составляется на КОНЕЦ периода.

    ⚠️ ИЗВЕСТНОЕ ОГРАНИЧЕНИЕ, оставлено сознательно. Если документ датирован
    только началом периода (чистая форма РСБУ «на 1 октября»), концов среди
    кандидатов не будет вовсе — якоря нет, фильтр не работает, поведение как
    было. Случая, где начало и конец смешаны в одном документе, у нас нет ни
    одного; городить под него машинерию значило бы писать код под догадку.
    """
    anchor = max((k for k in (_point_period_key(p) for p in periods)
                  if k and (k[1], k[2]) not in _PERIOD_START_DAYS), default=None)
    if anchor is None:
        return periods
    out = [p for p in periods
           if (_period_end_key(p) or anchor) <= anchor]
    # Пустой результат означает, что мы отфильтровали всё, — такого быть не
    # должно (сама якорная дата проходит проверку), но если случится, отдаём
    # вход: молча остаться без периодов хуже, чем оставить лишний.
    return out or periods













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
    ordered = sort_periods(_prefer_financial_report_periods(sort_periods(periods)))
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
    r"budgets?|targets?|planned|plans?|business\s+plan|medium[-\s]?term\s+plan|"
    r"scenarios?|sensitivity)\b"
    r"|(?:прогноз|прогнозн|планов|плановый|план|ориентир|сценари)",
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

# ⬇️ ДОСЛОВНО ИЗ FINENGINE (#257). Здесь стоял наш суженный
# `_relative_duration_period_from_header`: он понимал ТОЛЬКО ось потока
# («текущий»/«предыдущий» период) и молчал на балансовых заголовках. Их
# версия покрывает обе оси и вдобавок читает регуляторные формы РСБУ
# («на начало отчётного года», «отстоящую на два квартала»), которых у нас
# не было вовсе. Имя тоже их: `_relative_metric_period_from_header`.
_RELATIVE_COMPARATIVE_PERIOD_RE = re.compile(
    r"(?:\bcorresponding\b.{0,40}\b(?:prior|previous)\b|"
    r"\bсоответствующ\w*\b.{0,50}\bпрошл\w*\b)",
    re.I,
)

_RELATIVE_START_PREVIOUS_YEAR_RE = re.compile(
    r"\b(?:на\s+)?начал\w*\s+предыдущ\w*\s+отчетн\w*\s+год\w*\b",
    re.I,
)

_RELATIVE_START_CURRENT_YEAR_RE = re.compile(
    r"\b(?:на\s+)?начал\w*\s+отчетн\w*\s+год\w*\b",
    re.I,
)

_RELATIVE_QUARTERS_BACK_RE = re.compile(
    r"\bотстоящ\w*\s+на\s+(один|одного|два|двух|три|трех|четыре|четырех|[1-4])\s+квартал",
    re.I,
)

def _shift_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))

_RSBU_REGULATORY_PERIOD_START_ENUMS = {
    "COMMON_EQUITY_TIER_ONE_CAPITAL",
    "RISK_WEIGHTED_ASSETS",
    "CET1_RATIO_DISCLOSED",
    "TIER1_RATIO_DISCLOSED",
}

def _relative_point_anchor(label: str, *, period_start: bool = False) -> date | None:
    """Turn the run period into the point date implied by an RAS form header."""
    normalized = normalize_period_label(label)
    if match := re.fullmatch(r"(\d{1,2})\.(\d{2})\.(20\d{2})", normalized):
        day, month, year = map(int, match.groups())
        anchor = date(year, month, day)
        # Russian bank forms may be dated on the first day after the reporting
        # quarter/year. Generic balance-sheet columns belong to the preceding
        # period end; regulatory capital disclosures preserve the literal
        # first-day reporting date.
        if period_start:
            return anchor
        return anchor - timedelta(days=1) if day == 1 and month in {1, 4, 7, 10} else anchor
    if match := re.fullmatch(r"Q([1-4]) (20\d{2})", normalized):
        quarter, year = map(int, match.groups())
        month = quarter * 3
        anchor = date(year, month, calendar.monthrange(year, month)[1])
        return anchor + timedelta(days=1) if period_start else anchor
    if match := re.fullmatch(r"([369])M (20\d{2})", normalized):
        months, year = map(int, match.groups())
        anchor = date(year, months, calendar.monthrange(year, months)[1])
        return anchor + timedelta(days=1) if period_start else anchor
    if match := re.fullmatch(r"FY (20\d{2})", normalized):
        anchor = date(int(match.group(1)), 12, 31)
        return anchor + timedelta(days=1) if period_start else anchor
    if match := re.fullmatch(r"12M (\d{1,2})\.(\d{2})\.(20\d{2})", normalized):
        day, month, year = map(int, match.groups())
        anchor = date(year, month, day)
        if period_start:
            return anchor
        return anchor - timedelta(days=1) if day == 1 and month in {1, 4, 7, 10} else anchor
    return None

def _duration_from_point_anchor(label: str) -> str:
    normalized = normalize_period_label(label, value_kind=DURATION_KIND)
    if re.fullmatch(r"(?:FY|Q[1-4]|[369]M) 20\d{2}", normalized):
        return normalized
    anchor = _relative_point_anchor(label)
    if anchor is None:
        return normalized
    if (anchor.month, anchor.day) == (12, 31):
        return f"FY {anchor.year}"
    if (anchor.month, anchor.day) in {(3, 31), (6, 30), (9, 30)}:
        return f"{anchor.month}M {anchor.year}" if anchor.month != 3 else f"Q1 {anchor.year}"
    return normalized

def _relative_metric_period_from_header(
    raw_period: object,
    *,
    enum: str | None,
    period_current: str,
    period_prev: str,
) -> str:
    """Map generic Russian/English form headers to concrete run periods."""
    if not enum:
        return ""
    text = str(raw_period or "").replace("\u00a0", " ").strip().lower().replace("ё", "е")
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    kind = period_kind_for_enum(enum)
    # A model may compress a report column to a bare year.  A bare year is
    # not an annual-period declaration when the run itself is interim: keep
    # the run's axis instead of inventing FY/31.12 for the current year.
    bare_year = re.fullmatch(r"(20\d{2})(?:\s*г\.?)?", text)
    anchor = _relative_point_anchor(period_current)
    if bare_year and anchor is not None:
        year = int(bare_year.group(1))
        if kind == DURATION_KIND:
            current = _duration_from_point_anchor(period_current)
            if year == anchor.year:
                return current
            if year == anchor.year - 1:
                return previous_period_label(current)
        elif kind == POINT_KIND:
            if year == anchor.year:
                return anchor.strftime("%d.%m.%Y")
            if year == anchor.year - 1:
                return date(year, 12, 31).strftime("%d.%m.%Y")
    if kind == DURATION_KIND:
        current = _duration_from_point_anchor(period_current)
        previous = previous_period_label(current)
        if _RELATIVE_COMPARATIVE_PERIOD_RE.search(text) or _RELATIVE_PREVIOUS_PERIOD_RE.search(text):
            return previous
        if _RELATIVE_CURRENT_PERIOD_RE.search(text):
            return current
        return ""
    if kind != POINT_KIND:
        return ""

    regulatory_period_start = str(enum or "").strip().upper() in _RSBU_REGULATORY_PERIOD_START_ENUMS
    anchor = _relative_point_anchor(period_current, period_start=regulatory_period_start)
    if anchor is None:
        return ""
    regulatory_year_start = anchor.year - 1 if (anchor.month, anchor.day) == (1, 1) else anchor.year
    if _RELATIVE_START_PREVIOUS_YEAR_RE.search(text):
        if regulatory_period_start:
            return date(regulatory_year_start - 1, 1, 1).strftime("%d.%m.%Y")
        return date(anchor.year - 2, 12, 31).strftime("%d.%m.%Y")
    if _RELATIVE_START_CURRENT_YEAR_RE.search(text) or _RELATIVE_PREVIOUS_PERIOD_RE.search(text):
        if regulatory_period_start:
            return date(regulatory_year_start, 1, 1).strftime("%d.%m.%Y")
        return date(anchor.year - 1, 12, 31).strftime("%d.%m.%Y")
    if match := _RELATIVE_QUARTERS_BACK_RE.search(text):
        words = {"один": 1, "одного": 1, "два": 2, "двух": 2, "три": 3, "трех": 3, "четыре": 4, "четырех": 4}
        quarters = words.get(match.group(1), int(match.group(1)) if match.group(1).isdigit() else 0)
        return _shift_months(anchor, -3 * quarters).strftime("%d.%m.%Y")
    if _RELATIVE_CURRENT_PERIOD_RE.search(text) or re.search(r"\bотчетн\w*\s+дат\w*\b", text):
        return anchor.strftime("%d.%m.%Y")
    return ""



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


# ⚠️ РАСХОЖДЕНИЕ С РАСКЛАДКОЙ АЛЕКСЕЯ, СОЗНАТЕЛЬНОЕ. У него эта функция
# лежит в company_detection — там, где она стояла в монолите, по соседству с
# определением компании. Но определяет она ГОД, то есть период; а зовёт её
# `_select_run_periods` из этого модуля. Оставь её в company_detection — и
# period_selection ссылался бы вперёд по порядку зависимостей, разбиение
# уткнулось бы в круг импортов. Поймано ruff'ом (F821), а не глазами.


# ПЕРЕНЕСЕНО ИЗ FINENGINE (origin/main, v634) для двухстадийного извлечения #257.
# Единственный символ из тридцати трёх, которого у нас не было: остальные
# тридцать два `two_stage_extractor` берёт из движка как есть.
#
# Что делает: детерминированный детектор года иногда принимает квартал из
# повествования за отчётный период. Если достаточная доля запрошенных
# показателей имеет РЕАЛЬНЫЕ значения в более поздних непрогнозных колонках —
# граница отчётного года двигается на самый подкреплённый поздний год.
# То есть широкое свидетельство модели перевешивает устаревшую отсечку.
#
# Код перенесён ДОСЛОВНО. Зависимости — наши: `_is_forward_looking_period_header`
# и `_period_year` из этого модуля, `_parse_verbatim_value` из value_parsing.

# Доля запрошенных показателей, которая должна показать фактические значения в
# более позднем году, прежде чем этот год примут за границу отчёта
# (см. `_model_supported_max_report_year`).
_LATER_YEAR_SUPPORT_SHARE = 0.35


def _model_supported_max_report_year(
    parsed_by_enum: Mapping[str, object],
    *,
    detected_max_report_year: int | None,
    total_metrics: int,
) -> tuple[int | None, dict[int, int], int]:
    """Let broad model evidence overrule a stale deterministic year cutoff.

    A narrative quarter can be mistaken for the report period. If a large
    enough share of the requested metrics carries actual values in later,
    non-forecast columns, the most-supported later year is a safer report
    boundary.

    Порог — 35%, а не большинство: ошибаясь, отсечка режет ЖИВЫЕ периоды, а
    треть реестра, сошедшаяся на позднем годе, — свидетельство куда весомее
    одного заголовка, который читает детерминированный якорь. Прогнозные
    колонки здесь не голосуют — они отфильтрованы выше.
    """
    threshold = max(1, math.ceil(max(0, total_metrics) * _LATER_YEAR_SUPPORT_SHARE))
    if detected_max_report_year is None or total_metrics <= 0:
        return detected_max_report_year, {}, threshold

    enums_by_year: dict[int, set[str]] = {}
    future_enums: set[str] = set()
    for raw_enum, raw_info in parsed_by_enum.items():
        if not isinstance(raw_info, dict):
            continue
        enum = str(raw_enum).strip().upper()
        metric_years: set[int] = set()
        values = raw_info.get("values")
        if isinstance(values, dict):
            raw_items = values.items()
        else:
            raw_items = ()
        period_values = raw_info.get("period_values")
        list_items = (
            (
                item.get("period") or item.get("label") or "",
                item.get("value_text", item.get("value")),
            )
            for item in period_values
            if isinstance(item, dict)
        ) if isinstance(period_values, list) else ()
        for raw_period, raw_value in (*raw_items, *list_items):
            year = _period_year(raw_period)
            if (
                year is None
                or year <= detected_max_report_year
                or _is_forward_looking_period_header(raw_period)
                or _parse_verbatim_value(raw_value) is None
            ):
                continue
            metric_years.add(year)
        for year in metric_years:
            enums_by_year.setdefault(year, set()).add(enum)
            future_enums.add(enum)

    support_by_year = {year: len(enums) for year, enums in sorted(enums_by_year.items())}
    if len(future_enums) < threshold or not support_by_year:
        return detected_max_report_year, support_by_year, threshold

    strongest_support = max(support_by_year.values())
    trusted_year = max(year for year, support in support_by_year.items() if support == strongest_support)
    return trusted_year, support_by_year, threshold

# ══════════════════════════════════════════════════════════════════════
# ВЫБОР ПЕРИОДОВ ПО МЕХАНИКЕ FINENGINE (решение владельца 16.08, #257).
# Перенесено из origin/main ДОСЛОВНО: 26 объявлений замыкания
# `_select_run_periods` — 15 заменили наши, 11 добавились.
#
# ЧТО ПРИШЛО НОВОГО: календарь периодов (`period_calendar`), индийский
# финансовый год, приоритет колонок отчёта при столкновении ключей,
# нормализация меток по календарю, учёт `FiscalPeriodContext`.
#
# ⚠️ `_drop_periods_that_have_not_closed` (наша правка #255) В ИХ
# ЗАМЫКАНИЕ НЕ ВХОДИТ и больше не вызывается. Снято сознательно по
# решению владельца: «забей на вчерашние правки, делай как у них».
# ══════════════════════════════════════════════════════════════════════

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

def _normalize_period_calendar_labels(periods: list[str], period_calendar: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for period in periods:
        norm = _normalize_period_calendar_label(period, period_calendar)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out

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
        return (month, day) in {
            (1, 1),
            (3, 31),
            (4, 1),
            (6, 30),
            (7, 1),
            (9, 30),
            (10, 1),
            (12, 31),
        }
    return False

def _prefer_financial_report_periods(periods: list[str]) -> list[str]:
    preferred = [period for period in periods if _is_preferred_report_period(period)]
    return preferred or periods

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

    🔧 ПРИЗНАК СУЖЕН ДО ВЕДУЩЕГО КАНДИДАТА (#259). У них промежуточным считался
    отчёт, в котором ГДЕ-УГОДНО встретился квартал Q1–Q3. В годовом отчёте это
    обычная поквартальная сноска, и правило било по своим: на 119 документах
    стенда восемь годовых превращались из `FY 2023` в `Q4 2023`, а ТМК-2021 —
    в `Q1 2021`. Цена ошибки не косметическая: `FY` это 12 месяцев, `Q4` — три,
    и каждый потоковый показатель начинает значить вчетверо меньше.

    Замысел их правила при этом сохранён: отбрасывать годовую сноску нужно у
    ПРОМЕЖУТОЧНОГО отчёта. Промежуточность определяется по первому кандидату —
    он и есть предполагаемый период прогона, — а не по любому упоминанию.
    Случай БСПб (#255) под правило по-прежнему подпадает: там ведущим стоит
    полугодие.
    """
    leading_quarter = _interim_quarter_for_run_period(periods[0]) if periods else None
    if leading_quarter is None or leading_quarter >= 4:
        return periods
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

def _date_anchor_year(label: object) -> int | None:
    period = normalize_period_label(label)
    return _period_year(period) if re.fullmatch(r"\d{1,2}\.\d{2}\.20\d{2}", period) else None

def _annual_anchor_year(label: object) -> int | None:
    period = normalize_period_label(label)
    if re.fullmatch(r"FY 20\d{2}", period):
        return _period_year(period)
    if re.fullmatch(r"12M (?:20\d{2}|\d{1,2}\.\d{2}\.20\d{2})", period):
        return _period_year(period)
    return None

def _duration_anchor_year(label: object) -> int | None:
    period = normalize_period_label(label)
    if re.fullmatch(r"Q[1-4] 20\d{2}", period):
        return _period_year(period)
    if re.fullmatch(r"[69]M 20\d{2}", period):
        return _period_year(period)
    if re.fullmatch(r"\d{1,2}M \d{1,2}\.\d{2}\.20\d{2}", period):
        return _period_year(period)
    return None

def _voting_date_anchor_years(periods: list[str], horizon: int | None) -> list[int]:
    """Годы дат, которым позволено голосовать за якорь отчёта.

    Голая дата обязана голосовать — иначе годовой отчёт теряет свой год (см.
    `_max_report_anchor_year`). Но `detect_period_candidates` читает весь текст,
    и среди дат приезжают сроки погашения: у МКБ `31.12.2064`, у Самсунга
    `31.12.2049`, у БСПб `31.12.2037`. Пустив их голосовать без разбора, мы
    получаем якорь 2064 — а он ничего не отсекает вовсе, и периодом прогона
    становится срок погашения облигации. Замер на 85 golden-документах с PDF:
    дословный перенос уводит период прогона в дату из примечания на СЕМИ.

    Горизонт — самый поздний год отрезков и годовых подписей. До него даты
    голосуют как есть. Перешагнуть его дата вправе только на ОДИН год и только
    будучи концом квартала: баланс датируют 31.03/30.06/30.09/31.12, а дата
    подписания, события или погашения такой формы не имеет. Этого хватает и на
    случай ВК (`31.12.2025` при отрезках только 2024 года), и на то, чтобы
    `06.08.2027` из графика погашений не сдвинул отчёт за третий квартал 2025.

    ⚠️ ЦЕНА ЭТОЙ ФОРТОЧКИ, ЗАМЕРЕНА И НЕ ЗАКРЫТА. На Уралсибе-2024 в неё
    пролезает `31.12.2025`, и период прогона уезжает с `FY 2024` на `31.12.2025`
    — ровно тот же класс ошибки, только в другую сторону. Виноват не якорь:
    извлекатель текста расклеил таблицу по колонкам, строка `На 31 декабря`
    встала рядом со значением ячейки `25`, и `detect_period_candidates` склеила
    их в дату. Чинить это надо в детекторе, а не сужением форточки: сузить её
    так, чтобы Уралсиб не пролез, значит не пустить и Дойчбанк.
    """
    date_years = [year for p in periods for year in [_date_anchor_year(p)] if year is not None]
    if horizon is None:
        return date_years
    extended = {
        _period_year(p)
        for p in periods
        for key in [_point_period_key(p)]
        if key is not None and (key[1], key[2]) in _PERIOD_END_DAYS
    }
    return [year for year in date_years if year <= horizon or (year == horizon + 1 and year in extended)]

def _max_report_anchor_year(periods: list[str]) -> int | None:
    """Самый поздний год, О КОТОРОМ отчёт. Голосуют все виды якорей.

    Голая дата баланса в «сильные» не входила: сильными считались длительности
    и годовые подписи, подтверждённые датой того же года. Годовой отчёт, где
    распознались `31.12.2025`, `31.12.2024` и `Q4 2024` из примечания, якорился
    на 2024 — и `_filter_plausible_report_periods` отбрасывала ВСЕ периоды 2025
    как будущий год из таблицы погашений. Отчёт возвращался с прошлым годом в
    колонке текущего: у ВК-2025 выручка 147 573 вместо 159 960, стоявшего в той
    же таблице рядом. Худший класс ошибки — числа верные, ответ выглядит рабочим.

    Промежуточные отчёты это не задевало по случайности: `6M 2025` сам по себе
    длительность, и ранняя ветка попадала в нужный год. У годового отчёта своей
    длительности нет.

    Годовая подпись по-прежнему уступает дате того же года, когда такая дата
    есть: `FY 2026` из примечания о годе вперёд не должна становиться якорем,
    а `FY 2025` рядом с `31.12.2025` — должна.
    """
    duration_years = [year for p in periods for year in [_duration_anchor_year(p)] if year is not None]
    annual_years = [year for p in periods for year in [_annual_anchor_year(p)] if year is not None]
    date_years = _voting_date_anchor_years(periods, max(duration_years + annual_years, default=None))
    candidates = duration_years + date_years
    if annual_years:
        matched_annual_years = [year for year in annual_years if year in date_years]
        candidates += matched_annual_years or annual_years
    return max(candidates) if candidates else None

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

def _is_indian_fiscal_calendar(period_calendar: str) -> bool:
    return str(period_calendar or "").strip().lower().replace("-", "_") == "indian_fiscal"

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
        # Russian regulatory bank forms use the first day after a reporting
        # period (01.04/01.07/01.10/01.01) as the form date. Treat it as the
        # same report column as the quarter/year that just ended, while
        # preserving the literal date as the public point-in-time label.
        if day == 1 and month in {1, 4, 7, 10}:
            if month == 1:
                return ("annual", year - 1, 0)
            return ("quarter", year, (month - 1) // 3)
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

_POINT_PERIOD_RE = re.compile(r"^(\d{1,2})[.](\d{1,2})[.](20\d{2})$")

def _point_period_key(period: object) -> tuple[int, int, int] | None:
    """Return sortable key only for normalized point-in-time date labels."""
    p = normalize_period_label(period)
    m = _POINT_PERIOD_RE.fullmatch(p)
    if not m:
        return None
    day, month, year = map(int, m.groups())
    return year, month, day

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

def _period_year(label: object) -> int | None:
    period = normalize_period_label(label)
    m = re.search(r"(20\d{2})", period)
    return int(m.group(1)) if m else None

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
    cover_year = _detect_year_from_text(pdf_text)
    detected_years = [year for period in detected for year in [_period_year(period)] if year is not None]
    if cover_year and (not detected_years or int(cover_year) > max(detected_years)):
        detected.append(normalize_period_label(cover_year, fiscal_context=fiscal_context))
    doc_norm = normalize_period_label(doc_period, fiscal_context=fiscal_context) if doc_period else ""
    periods = _filter_plausible_report_periods(sort_periods(([doc_norm] if doc_norm else []) + detected))
    if not periods:
        year = _detect_year_from_text(pdf_text) or ""
        if year:
            periods = [normalize_period_label(year, fiscal_context=fiscal_context)]
    # Вызов вернулся сюда после #259. Он стоял ровно на этом месте до замены
    # `_select_run_periods` на версию Finengine и вместе с ней пропал: у них
    # такого фильтра нет, потому что их `_is_preferred_report_period`
    # принимает первые числа кварталов, а незакрытый период отсекать нечем.
    periods = _drop_periods_that_have_not_closed(periods)
    periods = _drop_periods_ending_after_today(periods)
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
    periods = _prefer_period_named_on_the_cover(periods, pdf_text, fiscal_context)
    period_current = periods[0] if periods else (doc_norm or fallback_period_label)
    period_prev = _select_previous_run_period(period_current, periods, period_calendar)
    return periods, period_current, period_prev
