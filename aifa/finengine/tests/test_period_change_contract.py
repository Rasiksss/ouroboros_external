"""Карточка 32, T1: `PeriodChangeRow`/`PeriodChangeReport` не позволяют собрать
непоследовательный результат — процент без опоры, «сравнено» без второго значения,
готовое объяснение без текста."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from finengine.contracts import EvidenceRef, NarrativeOutput, PeriodChangeReport, PeriodChangeRow

_REFS = [EvidenceRef(evidence_id="ev-cur"), EvidenceRef(evidence_id="ev-prior")]


def _computed(**overrides) -> dict:
    row = dict(
        row_id="changes:REVENUE", order=0, concept="REVENUE", label="Выручка", status="computed",
        current_period_ref="FY 2025", prior_period_ref="FY 2024", current_value=1500.0,
        prior_value=1200.0, absolute_change=300.0, relative_change_pct=25.0, evidence_refs=_REFS,
    )
    row.update(overrides)
    return row


def _no_prior(**overrides) -> dict:
    row = dict(
        row_id="changes:REVENUE", order=0, concept="REVENUE", label="Выручка",
        status="no_prior_period", reason="предыдущий период не найден",
    )
    row.update(overrides)
    return row


def _report(**overrides) -> dict:
    report = dict(
        run_id="run-1", role="other", rows=[PeriodChangeRow(**_computed())],
        explanation=NarrativeOutput(run_id="run-1", text="Выручка выросла."),
        explanation_status="ready",
    )
    report.update(overrides)
    return report


def test_a_computed_row_with_both_values_and_two_evidence_refs_is_valid():
    row = PeriodChangeRow(**_computed())
    assert row.relative_change_pct == 25.0 and len(row.evidence_refs) == 2


@pytest.mark.parametrize("missing", [
    "current_value", "prior_value", "absolute_change", "current_period_ref", "prior_period_ref",
])
def test_a_compared_row_needs_both_values_both_periods_and_the_change(missing):
    with pytest.raises(ValidationError, match="requires both values"):
        PeriodChangeRow(**_computed(**{missing: None}))


def test_a_compared_row_needs_two_evidence_refs():
    with pytest.raises(ValidationError, match="two evidence refs"):
        PeriodChangeRow(**_computed(evidence_refs=_REFS[:1]))


def test_computed_needs_a_percentage_and_zero_base_forbids_one():
    with pytest.raises(ValidationError, match="requires relative_change_pct"):
        PeriodChangeRow(**_computed(relative_change_pct=None))
    with pytest.raises(ValidationError, match="cannot carry a percentage"):
        PeriodChangeRow(**_computed(status="zero_base", prior_value=0.0, reason="база 0"))
    zero = PeriodChangeRow(**_computed(
        status="zero_base", prior_value=0.0, relative_change_pct=None, reason="база 0",
    ))
    assert zero.relative_change_pct is None and zero.absolute_change == 300.0


@pytest.mark.parametrize("status", ["no_prior_period", "not_comparable"])
def test_a_row_without_comparison_needs_a_reason_and_cannot_carry_a_change(status):
    with pytest.raises(ValidationError, match="requires a reason"):
        PeriodChangeRow(**_no_prior(status=status, reason=""))
    with pytest.raises(ValidationError, match="cannot carry a change"):
        PeriodChangeRow(**_no_prior(status=status, absolute_change=5.0))
    with pytest.raises(ValidationError, match="cannot carry a change"):
        PeriodChangeRow(**_no_prior(status=status, relative_change_pct=5.0))


def test_a_report_with_a_ready_explanation_is_valid():
    report = PeriodChangeReport(**_report())
    assert report.output_type == "PeriodChangeReport" and report.explanation.text


def test_a_ready_explanation_needs_the_text_and_an_unavailable_one_needs_a_reason():
    with pytest.raises(ValidationError, match="requires an explanation"):
        PeriodChangeReport(**_report(explanation=None))
    with pytest.raises(ValidationError, match="requires explanation_note"):
        PeriodChangeReport(**_report(explanation=None, explanation_status="unavailable"))
    with pytest.raises(ValidationError, match="cannot carry an explanation"):
        PeriodChangeReport(**_report(explanation_status="unavailable", explanation_note="причина"))
    unavailable = PeriodChangeReport(**_report(
        explanation=None, explanation_status="unavailable", explanation_note="причина",
    ))
    assert unavailable.explanation is None


def test_report_rows_must_be_contiguous_from_zero():
    with pytest.raises(ValidationError, match="contiguous"):
        PeriodChangeReport(**_report(rows=[PeriodChangeRow(**_computed(order=1))]))
