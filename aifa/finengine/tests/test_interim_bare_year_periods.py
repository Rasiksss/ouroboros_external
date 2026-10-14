"""Bare year headers inherit the current interim report's time axis.

The model sometimes compresses ``30.06.2026``/``6M 2026`` to just ``2026``.
Without the run anchor this became the nonexistent ``31.12.2026``/``FY 2026``.
"""
from finengine.skills.bulk_extractor import _coerce_period_values


def _periods(enum: str) -> list[str]:
    values, _unit = _coerce_period_values(
        {"values": {"2026": "100", "2025": "90"}},
        enum=enum,
        period_current="6M 2026",
        period_prev="6M 2025",
        default_unit="тыс. руб.",
        max_report_year=2026,
    )
    return [item["period"] for item in values]


def test_bare_years_for_a_point_metric_use_report_end_and_previous_year_end():
    assert _periods("INVENTORIES") == ["30.06.2026", "31.12.2025"]


def test_bare_years_for_a_duration_metric_keep_the_interim_duration():
    assert _periods("SALES_REVENUE") == ["6M 2026", "6M 2025"]
