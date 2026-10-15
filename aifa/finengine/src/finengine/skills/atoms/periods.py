"""`id.periods` — канонические интервалы периодов, момент/интервал разграничены
явно (A6, docs/build-plan/11-srez-01-run-passport.md).

Адаптер над `finengine.skills.periods`: разбор/нормализация подписей периодов
не переписывается — только упаковка уже канонической метки в контракт
`PeriodAxis` (начало/конец как настоящие даты, а не строка). Нераспознанная
подпись не угадывается: она просто не попадает в результат (пробел
доказательств, не «ближайший год» — карточка `id.periods`).
"""
from __future__ import annotations

import hashlib
from datetime import date

from finengine.contracts import PeriodAxis
from finengine.skills.periods import (
    FiscalPeriodContext,
    _duration_components_from_label,
    _kind_is_point,
    _parse_date,
    normalize_period_label,
)


def _period_id(canonical_label: str, kind: str) -> str:
    return "per-" + hashlib.sha256(f"{kind}:{canonical_label}".encode("utf-8")).hexdigest()[:16]


def _start_of_duration(end: date, months: int) -> date:
    """Первый день интервала длиной `months`, закончившегося на `end`.

    Периоды в этом домене всегда начинаются с 1-го числа месяца — и
    календарные (Q/6M/9M/FY), и сдвинутые финансовые года (`12M 28.02.2025`
    начинается 01.03.2024). Формула работает по месяцам, день `end` не важен.
    """
    start_index = end.year * 12 + (end.month - 1) - months + 1
    year, month0 = divmod(start_index, 12)
    return date(year, month0 + 1, 1)


def _resolve_end_date(canonical: str, *, is_point: bool) -> date | None:
    if is_point:
        return _parse_date(canonical)
    duration = _duration_components_from_label(canonical)
    if duration:
        return duration[1]
    return _parse_date(canonical)


def period_axis_from_labels(
    labels: list[str] | tuple[str, ...],
    *,
    value_kind: str | None = None,
    fiscal_context: FiscalPeriodContext | None = None,
) -> list[PeriodAxis]:
    """Превратить сырые подписи периодов в канонические `PeriodAxis`.

    `value_kind` разграничивает момент/интервал — та же классификация, что
    использует остальной разбор периодов (`_kind_is_point`). Дубликаты после
    нормализации схлопываются: одна подпись — одна ось.
    """
    fiscal_context = fiscal_context or FiscalPeriodContext.empty()
    is_point = _kind_is_point(value_kind)
    axes: list[PeriodAxis] = []
    seen: set[str] = set()
    for raw in labels:
        canonical = normalize_period_label(raw, value_kind=value_kind, fiscal_context=fiscal_context)
        if not canonical or canonical in seen:
            continue
        end = _resolve_end_date(canonical, is_point=is_point)
        if end is None:
            continue  # нераспознанная дата — пробел доказательств, не угадываем
        start = None
        if not is_point:
            duration = _duration_components_from_label(canonical)
            months = duration[0] if duration else 12
            start = _start_of_duration(end, months)
        axes.append(
            PeriodAxis(
                period_id=_period_id(canonical, "instant" if is_point else "duration"),
                kind="instant" if is_point else "duration",
                start=start,
                end=end,
            )
        )
        seen.add(canonical)
    return axes
