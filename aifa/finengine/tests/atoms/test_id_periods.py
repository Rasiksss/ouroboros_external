"""`id.periods` — критерий готовности (docs/build-plan/11-srez-01-run-passport.md):

1. Отчётные даты, интервалы и сравнительные периоды извлекаются без
   перестановки текущего/предыдущего периода (здесь: на размеченном наборе
   подписей — «не менее 50 документов» относится к golden-набору `extraction`,
   которого в этой сессии ещё нет (PHASE-0-STATUS.md); атом проверяется на
   размеченных подписях периодов, которые он и принимает на вход).
2. Моментные и интервальные показатели различаются явно; годовой и
   промежуточный периоды не смешиваются.
3. Неоднозначный период сохраняется как неоднозначный, не угадывается.
4. Периоды имеют источник (`period_id`) и однозначно сопоставляются фактам.

Сценарии: год, полугодие, квартал, нестандартный финансовый год, високосный
год, моментный и интервальный показатель.
"""
from __future__ import annotations

from datetime import date

from finengine.skills.atoms.periods import period_axis_from_labels
from finengine.skills.periods import FiscalPeriodContext


def test_calendar_year_duration():
    axes = period_axis_from_labels(["FY 2025"], value_kind="duration")
    assert len(axes) == 1
    assert axes[0].kind == "duration"
    assert axes[0].start == date(2025, 1, 1)
    assert axes[0].end == date(2025, 12, 31)


def test_half_year_duration():
    axes = period_axis_from_labels(["6 месяцев 2025"], value_kind="duration")
    assert axes[0].start == date(2025, 1, 1)
    assert axes[0].end == date(2025, 6, 30)


def test_quarter_duration():
    axes = period_axis_from_labels(["Q1 2025"], value_kind="duration")
    assert axes[0].start == date(2025, 1, 1)
    assert axes[0].end == date(2025, 3, 31)


def test_shifted_fiscal_year_duration_uses_the_document_context():
    ctx = FiscalPeriodContext({2025: date(2025, 2, 28)})
    axes = period_axis_from_labels(["FY 2025"], value_kind="duration", fiscal_context=ctx)
    assert axes[0].end == date(2025, 2, 28)
    assert axes[0].start == date(2024, 3, 1)


def test_leap_year_point_in_time_date_is_preserved():
    axes = period_axis_from_labels(["29.02.2024"], value_kind="point")
    assert axes[0].kind == "instant"
    assert axes[0].start is None
    assert axes[0].end == date(2024, 2, 29)


def test_point_and_duration_metrics_are_not_conflated_for_the_same_label():
    point = period_axis_from_labels(["FY 2025"], value_kind="point")
    duration = period_axis_from_labels(["FY 2025"], value_kind="duration")
    assert point[0].kind == "instant"
    assert point[0].start is None
    assert duration[0].kind == "duration"
    assert duration[0].start is not None
    assert point[0].end != duration[0].start


def test_unrecognized_label_produces_a_gap_not_a_guess():
    axes = period_axis_from_labels(["совершенно нераспознаваемая строка"], value_kind="duration")
    assert axes == []


def test_duplicate_labels_collapse_to_one_axis():
    axes = period_axis_from_labels(["FY 2025", "2025 год"], value_kind="duration")
    assert len(axes) == 1


def test_period_id_is_a_stable_source_for_matching_facts():
    first = period_axis_from_labels(["FY 2025"], value_kind="duration")
    second = period_axis_from_labels(["FY 2025"], value_kind="duration")
    assert first[0].period_id == second[0].period_id
    assert first[0] == second[0]
