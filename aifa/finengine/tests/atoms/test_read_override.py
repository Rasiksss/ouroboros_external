"""Pure atom contract checks for Slice 10 `read.override`."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from finengine.contracts import CorrectionIntent, ExtractedMetric, ScalarFact
from finengine.skills.atoms.read_override import create_override, validate_correction


def _metric() -> ExtractedMetric:
    return ExtractedMetric(
        scalar_fact=ScalarFact(
            produced_by_atom="read.value", produced_by_atom_version="1.0.0",
            source_ref="doc", document_version="sha256:abc", concept="SALES",
            period_ref="2025", basis="IFRS", value=100.0, unit="RUB",
        ),
        concept="SALES", period_ref="2025",
    )


def test_create_override_keeps_original_evidence_immutable_and_auditable():
    original = _metric()
    before = original.model_dump(mode="json")
    correction = CorrectionIntent(
        action="apply", concept="SALES", evidence_id=original.scalar_fact.evidence_id,
        value=120.0, unit="RUB", period_ref="2025", basis="IFRS",
        reason="confirmed by user", original_wording="Нет, значение 120 RUB",
    )
    override, wrapper = create_override(original, correction, owner_id="alice", case_id="case-1")

    assert original.model_dump(mode="json") == before
    assert override.evidence_id != original.scalar_fact.evidence_id
    assert override.supersedes == original.scalar_fact.evidence_id
    assert override.overridden_evidence_id == original.scalar_fact.evidence_id
    assert override.owner_id == "alice"
    assert override.case_id == "case-1"
    assert override.original_wording == "Нет, значение 120 RUB"
    assert wrapper.human_override == override
    with pytest.raises(ValidationError):
        original.scalar_fact.value = 999  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("unit", "USD", "unit mismatch"), ("period_ref", "2024", "period mismatch"), ("basis", "RAS", "basis mismatch")],
)
def test_override_dimension_mismatch_is_explicit(field, value, message):
    correction = CorrectionIntent(
        action="apply", concept="SALES", value=120.0,
        original_wording="поправка", **{field: value},
    )
    with pytest.raises(ValueError, match=message):
        validate_correction(_metric(), correction)
