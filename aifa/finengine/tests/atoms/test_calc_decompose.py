from __future__ import annotations

from dataclasses import dataclass

import pytest

from finengine.skills.atoms.subject_history import (
    UnsupportedMethodologyError,
    adapt_factor_results,
    decompose_change,
)


def test_exact_additive_math_and_explicit_residual():
    result = decompose_change(
        concept="REVENUE", target_change=30, target_input_evidence_ids=["old", "new"],
        methodology_id="additive_v1", methodology_version="1",
        components=[
            {"component_id": "volume", "value": 10, "input_evidence_ids": ["q0", "q1"]},
            {"component_id": "price", "value": 20, "input_evidence_ids": ["p0", "p1"]},
        ],
    )
    assert result.complete is True and result.residual == 0
    assert [c.derived_fact.inputs for c in result.components] == [["q0", "q1"], ["p0", "p1"]]
    assert result.components[0].derived_fact.formula_version == "1"


def test_missing_component_is_incomplete_and_residual_is_not_a_fake_factor():
    result = decompose_change(
        concept="REVENUE", target_change=30, target_input_evidence_ids=["old", "new"],
        methodology_id="additive_v1", methodology_version="1",
        required_component_ids=["volume", "price"],
        components=[{"component_id": "volume", "value": 10, "input_evidence_ids": ["q"]}],
    )
    assert result.complete is False and result.missing_components == ["price"]
    assert result.residual == 20
    assert "residual" not in [c.component_id for c in result.components]


def test_unknown_methodology_fails_closed():
    with pytest.raises(UnsupportedMethodologyError):
        decompose_change(
            concept="X", target_change=1, target_input_evidence_ids=["a"],
            methodology_id="model_explanation", methodology_version="1",
            components=[],
        )


@dataclass
class _Factor:
    id: str
    name: str
    value: float
    formula_repr: str
    method: str = "chain"


def test_existing_factor_result_adapter_preserves_exact_math():
    result = adapt_factor_results(
        concept="REVENUE", target_change=5, target_input_evidence_ids=["a", "b"],
        factors=[_Factor("volume", "Volume", 2, "R1-R0"), _Factor("price", "Price", 3, "R2-R1")],
    )
    assert result.complete and result.residual == 0
    assert [x.value for x in result.components] == [2, 3]
