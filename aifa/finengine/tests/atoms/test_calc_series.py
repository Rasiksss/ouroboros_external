from __future__ import annotations

import pytest

from finengine.skills.atoms.subject_history import IncomparableSeriesError, build_series
from tests.slice11_fixtures import metric


def test_series_math_gaps_and_no_silent_interpolation():
    result = build_series([
        metric(100, period="2025-Q1"),
        metric(121, period="2025-Q3"),
    ])
    assert [p.period_ref for p in result.points] == ["2025-Q1", "2025-Q3"]
    assert result.gaps[0].missing_period_refs == ["2025-Q2"]
    assert result.interpolation_applied is False
    assert result.changes[0].absolute_change == 21
    assert result.changes[0].relative_change == pytest.approx(0.21)
    assert result.changes[0].annualized_rate == pytest.approx(0.4641)
    assert result.changes[0].input_evidence_ids == result.input_evidence_ids


def test_series_rejects_basis_or_unit_mismatch():
    with pytest.raises(IncomparableSeriesError):
        build_series([
            metric(100, period="2025-Q1", basis="IFRS"),
            metric(110, period="2025-Q2", basis="RAP"),
        ])


def test_zero_base_has_no_invented_relative_rate():
    result = build_series([metric(0, period="2025-Q1"), metric(10, period="2025-Q2")])
    assert result.changes[0].relative_change is None
    assert result.changes[0].annualized_rate is None
