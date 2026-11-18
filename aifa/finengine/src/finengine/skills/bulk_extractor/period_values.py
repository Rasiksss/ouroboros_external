"""Таблица «период → значение» и доступ к ней.

У показателя ДВЕ ОСИ ВРЕМЕНИ (#205 п.а). Запас (баланс) живёт по ДАТЕ —
`31.12.2020`; поток (ОПУ) живёт по ОТРЕЗКУ — `FY 2020`.

⚠️ ТЕЛА ФУНКЦИЙ — ДОСЛОВНО ИЗ FINENGINE (#257, решение владельца 16.08).
Что это дало сверх нашего снимка:

  * `period_calendar` и `fiscal_context` в разборе — то самое, на чём падала
    двухстадийная схема («unexpected keyword argument 'period_calendar'»);
  * валюта берётся у КОЛОНКИ, а не у общей шапки: в двуязычной шапке
    «millions of Korean won, thousands of US dollars» два курса, и каждое
    значение наследует свой;
  * индийский финансовый год в сверке периода (`_indian_fiscal_period_end_key`).

⚠️ ОДНА ФУНКЦИЯ ВЗЯТА НЕ ЦЕЛИКОМ — `_value_for_period`, разбор в ней самой.
Расхождение здесь двустороннее: у них есть индийский финансовый год, у нас —
проход по дате окончания (#205). Замена в лоб красит сторож
`test_previous_period_survives_the_other_axis` двумя случаями, поэтому взяты
ОБА прохода. Проверено прогоном набора: 86/1443 — при замене «как у них»
было 88/1441.
"""
from __future__ import annotations

from typing import Optional

from finengine.skills.period_validation import normalize_metric_period
from finengine.skills.periods import (
    DURATION_KIND,
    FiscalPeriodContext,
    normalize_incomplete_duration_label,
    normalize_period_label,
    period_kind_for_enum,
    sort_periods,
)

from .period_selection import (
    _duration_from_point_anchor,
    _indian_fiscal_period_end_key,
    _is_forward_looking_period_header,
    _is_indian_fiscal_calendar,
    _normalize_period_calendar_label,
    _period_end_key,
    _period_year,
    _relative_metric_period_from_header,
)
from .value_parsing import (
    _currency_from_unit_text,
    _is_percentage_unit_text,
    _normalise_eps_unit,
    _normalise_money_value,
    _parse_unit_text,
    _parse_verbatim_value,
    _primary_currency_from_unit_text,
    _unit_text_for_currency,
)


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
    primary_currency = _primary_currency_from_unit_text(unit_text_raw) or default_currency
    metric_unit = _unit_text_for_currency(unit_text_raw, primary_currency, default_currency) or unit_text_raw or default_unit or ""
    if metric_enum == "EPS":
        metric_unit = _normalise_eps_unit(unit_text_raw, default_unit)

    raw_items: list[tuple[str, object, str, int | None, str]] = []
    values = info.get("values")
    has_explicit_values = isinstance(values, dict)
    if isinstance(values, dict):
        for p, raw in values.items():
            period_currency = _currency_from_unit_text(p)
            raw_unit = _unit_text_for_currency(
                unit_text_raw,
                period_currency or primary_currency,
                default_currency,
            )
            raw_items.append((str(p), raw, raw_unit or metric_unit, None, str(raw) if raw is not None else ""))

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

    if not raw_items and not has_explicit_values:
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
        relative_period = _relative_metric_period_from_header(
            raw_period,
            enum=enum,
            period_current=period_current,
            period_prev=period_prev,
        )
        period_info = (
            None
            if relative_period
            else (
                normalize_metric_period(
                    raw_period,
                    enum,
                    drop_unrepairable=True,
                    fiscal_context=fiscal_context,
                )
                if enum
                else None
            )
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
            or (normalize_period_label(raw_period, fiscal_context=fiscal_context) if not enum else "")
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
        if metric_enum == "MODIFIED_AUDITORS_OPINION":
            period = _duration_from_point_anchor(period_current)
        if not period:
            continue
        period_year = _period_year(period)
        if max_report_year is not None and period_year is not None and period_year > max_report_year:
            continue
        if dash_as_zero and raw_value is not None and str(raw_value).replace("\u00a0", " ").strip() in {"", "-", "—", "–", "−"}:
            v = 0.0
        else:
            v = _parse_verbatim_value(
                raw_value,
                decimal_comma_three=_is_percentage_unit_text(raw_unit or metric_unit),
            )
        if metric_enum == "MODIFIED_AUDITORS_OPINION":
            unit = "boolean"
        elif metric_enum == "EPS":
            unit = _normalise_eps_unit(raw_unit or metric_unit, default_unit)
        else:
            v, norm_unit = _normalise_money_value(v, raw_unit or metric_unit)
            # 🔧 НАША ПРАВКА, ОТСТУПЛЕНИЕ ОТ ПОБАЙТОВОГО ПЕРЕНОСА (21.08).
            #
            # `_normalise_money_value` приводит ЧИСЛО к миллионам и переписывает
            # ярлык; числа нет — ярлык возвращается КАК ПРИШЁЛ, «млрд руб.».
            # Пустая клетка сама по себе безвредна, но ярлык показателя берётся
            # у ПЕРВОЙ строки периода (`period_values[0]["unit"]` — оба движка),
            # а первой стоит самый свежий период. Раскрытие «на 31 декабря 2024
            # года: 105,1 млрд рублей» без числа за текущую дату отдавало
            # показатель, у которого значение 105 100 приведено к миллионам, а
            # подпись говорит «млрд»: колонка «Единица» в выгрузке врёт в тысячу
            # раз на глазах у пользователя.
            #
            # Единица строки — единица её ЧИСЛА, и когда числа нет, ярлык всё
            # равно тот, в котором строка поехала бы с числом.
            if v is None:
                _, norm_unit = _normalise_money_value(0.0, raw_unit or metric_unit)
            unit = norm_unit or raw_unit or metric_unit
        item_currency = _currency_from_unit_text(raw_period) or _currency_from_unit_text(raw_unit)
        existing = by_period.get(period)
        if existing is not None:
            existing_currency = _currency_from_unit_text(existing.get("raw_period")) or _currency_from_unit_text(existing.get("unit"))
            # A translated USD/EUR column must not overwrite the primary/native
            # presentation column after both headers normalize to one period.
            if existing_currency == primary_currency:
                continue
            if item_currency != primary_currency:
                continue
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
    """Значение показателя за колонку отчёта `target`.

    🔧 ЕДИНСТВЕННОЕ СОЗНАТЕЛЬНОЕ РАСХОЖДЕНИЕ С FINENGINE В ЭТОМ ФАЙЛЕ, и оно
    подтверждено замером, а не рассуждением. У них проходов два: буквальная
    подпись и индийский финансовый год. Третий проход — по ДАТЕ ОКОНЧАНИЯ —
    наш (#205), и без него краснеет `test_previous_period_survives_the_other_axis`
    обоими случаями сразу:

        поток  «FY 2020»    ← спрашивают «31.12.2020»  (годовой отчёт Черкизово)
        запас  «31.12.2024» ← спрашивают «2024»        (баланс банка, все 38 строк)

    Периоды в таблице подписаны ПРАВИЛЬНО, и приведение к оси показателя тут ни
    при чём: промахивается сама сверка. Период прогона один на весь прогон и
    ложится на ОДНУ ось, а показатели живут на обеих (#205 п.а), поэтому
    буквального совпадения половине строк взять негде.

    Порядок проходов важен: индийский идёт вторым, как у них, наш — третьим,
    чтобы не менять их поведение там, где оно уже отвечает.
    """
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
    target_end = _period_end_key(target_norm)
    if target_end is None:
        return None
    for item in period_values:
        if _period_end_key(item.get("period")) == target_end:
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

