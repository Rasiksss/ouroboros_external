"""Срез 31, П6: без названного периода расчёт и проверочные гейты работают по
ВСЕМ доступным периодам, а не по первому найденному.

До среза: `calc.formula` сверял `main_period=""` с метками фактов и молча
возвращал пусто; `norm.unit`/`norm.time`/`verify.bounds`/`verify.identity`/
`verify.cross` пропускали ТОЛЬКО первое значение показателя (`x[0]`).
"""
from __future__ import annotations

import pytest

from finengine.contracts import (
    CalculatedMetric,
    ClassificationResult,
    DerivedFact,
    EntityIdentity,
    ExtractedMetric,
    RunPassport,
    ScalarFact,
    VerificationFinding,
)
from finengine.skills.atoms.calc_formula import calc_formula_executor
from finengine.skills.atoms.norm_time import norm_time_gate
from finengine.skills.atoms.norm_unit import norm_unit_gate
from finengine.skills.atoms.verify_bounds import verify_bounds_gate
from finengine.skills.atoms.verify_identity_cross import verify_cross_gate, verify_identity_gate
from finengine.skills.spec.atom_registry import NodeArgs

_REGISTRY = {
    "extractable": [{"enum": "REVENUE"}, {"enum": "COST_OF_SALES"}, {"enum": "PPE"}],
    "calculated": [
        ("GROSS_MARGIN", "Валовая маржа", "(REVENUE - COST_OF_SALES) / REVENUE", ["REVENUE", "COST_OF_SALES"]),
        ("TURNOVER", "Оборот", "REVENUE / PPE", ["REVENUE", "PPE"]),
    ],
    "calculated_meta": {},
}


def _passport() -> RunPassport:
    return RunPassport(
        goal_ir_id="goal-1",
        classification=ClassificationResult(
            profile_id="test-cell", standard="ifrs", company_type="non_financial", language="ru", reason="fixture",
        ),
        entity=EntityIdentity(entity_id="ent-1", canonical_name="Тест", raw_name="Тест"),
        registry_version="registry-7", methodology_version="methodology-1",
        model_version="model-1", prompt_version="prompt-1", sandbox_image_version="sandbox-1",
    )


def _extracted(concept: str, value: float, period: str) -> ExtractedMetric:
    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref=f"loc-{concept}-{period}",
        concept=concept, period_ref=period, value=value, unit="млн. руб.",
    )
    return ExtractedMetric(scalar_fact=fact, concept=concept, period_ref=period)


class _Ctx:
    def __init__(self, dependencies, run_passport=None, run_id="run-1"):
        self._deps = dependencies
        self.run_passport = run_passport
        self.run_id = run_id

    def dependencies(self):
        return tuple(self._deps)


@pytest.fixture(autouse=True)
def _patched_registry(monkeypatch):
    for target in (
        "finengine.skills.atoms.calc_formula.registry_for_passport",
        "finengine.skills.atoms.norm_unit.registry_for_passport",
        "finengine.skills.atoms.norm_time.registry_for_passport",
        "finengine.skills.atoms.verify_bounds.registry_for_passport",
        "finengine.skills.atoms.verify_identity_cross.registry_for_passport",
    ):
        monkeypatch.setattr(target, lambda passport: _REGISTRY)
    monkeypatch.setattr("finengine.skills.metric_schema.registry_for_profile", lambda pid: _REGISTRY)


# ---------------------------------------------------------------------------
# calc.formula
def test_a_formula_is_calculated_for_every_period_found_in_the_inputs():
    deps = [
        _extracted("REVENUE", 1000.0, "6M 2025"), _extracted("COST_OF_SALES", 750.0, "6M 2025"),
        _extracted("REVENUE", 2000.0, "6M 2026"), _extracted("COST_OF_SALES", 1000.0, "6M 2026"),
    ]
    result = calc_formula_executor(NodeArgs(concept="GROSS_MARGIN"), _Ctx(deps, _passport()))

    by_period = {m.period_ref: m.derived_fact.value for m in result if isinstance(m, CalculatedMetric)}
    assert set(by_period) == {"6M 2025", "6M 2026"}
    assert abs(by_period["6M 2025"] - 0.25) < 1e-9 and abs(by_period["6M 2026"] - 0.5) < 1e-9


def test_each_period_result_cites_only_the_inputs_of_its_own_period():
    a_rev, a_cost = _extracted("REVENUE", 1000.0, "6M 2025"), _extracted("COST_OF_SALES", 750.0, "6M 2025")
    b_rev, b_cost = _extracted("REVENUE", 2000.0, "6M 2026"), _extracted("COST_OF_SALES", 1000.0, "6M 2026")
    result = calc_formula_executor(NodeArgs(concept="GROSS_MARGIN"), _Ctx([a_rev, a_cost, b_rev, b_cost], _passport()))

    inputs = {m.period_ref: set(m.derived_fact.inputs) for m in result}
    assert inputs["6M 2025"] == {a_rev.scalar_fact.evidence_id, a_cost.scalar_fact.evidence_id}
    assert inputs["6M 2026"] == {b_rev.scalar_fact.evidence_id, b_cost.scalar_fact.evidence_id}


def test_a_period_where_an_input_is_missing_is_skipped_not_filled_with_zero():
    deps = [
        _extracted("REVENUE", 1000.0, "6M 2025"), _extracted("COST_OF_SALES", 750.0, "6M 2025"),
        _extracted("REVENUE", 2000.0, "6M 2026"),  # себестоимости за 2026 нет
    ]
    result = calc_formula_executor(NodeArgs(concept="GROSS_MARGIN"), _Ctx(deps, _passport()))

    assert [m.period_ref for m in result] == ["6M 2025"]


def test_a_flow_and_a_balance_of_the_same_period_make_one_result_not_two():
    """Поток («FY 2024») и остаток («31.12.2024») — один период."""
    deps = [_extracted("REVENUE", 1000.0, "FY 2024"), _extracted("PPE", 500.0, "31.12.2024")]
    result = calc_formula_executor(NodeArgs(concept="TURNOVER"), _Ctx(deps, _passport()))

    assert len(result) == 1
    assert abs(result[0].derived_fact.value - 2.0) < 1e-9


def test_no_inputs_at_all_gives_no_result():
    assert calc_formula_executor(NodeArgs(concept="GROSS_MARGIN"), _Ctx([], _passport())) == []


def test_a_requested_period_still_gives_exactly_that_period():
    deps = [
        _extracted("REVENUE", 1000.0, "6M 2025"), _extracted("COST_OF_SALES", 750.0, "6M 2025"),
        _extracted("REVENUE", 2000.0, "6M 2026"), _extracted("COST_OF_SALES", 1000.0, "6M 2026"),
    ]
    result = calc_formula_executor(NodeArgs(concept="GROSS_MARGIN", period_ref="6M 2026"), _Ctx(deps, _passport()))

    assert [m.period_ref for m in result] == ["6M 2026"]


# ---------------------------------------------------------------------------
# гейты: КАЖДОЕ значение, а не первое
def _ctx_for_gates(metrics):
    return _Ctx(metrics, _passport())


def test_the_extraction_gates_pass_every_period_of_a_concept():
    metrics = [_extracted("REVENUE", 100.0, "6M 2025"), _extracted("REVENUE", 200.0, "6M 2026")]
    args = NodeArgs(concept="REVENUE")
    for gate in (norm_unit_gate, norm_time_gate, verify_bounds_gate):
        out = gate(args, _ctx_for_gates(metrics))
        kept = [e for e in out if isinstance(e, ExtractedMetric)]
        assert sorted(m.period_ref for m in kept) == ["6M 2025", "6M 2026"], gate.__name__


def test_a_hard_bound_violation_drops_only_the_offending_period():
    registry = {
        **_REGISTRY, "calculated_meta": {"GROSS_MARGIN": {"min_value": 0.0, "max_value": 1.0}},
    }
    good = CalculatedMetric(
        derived_fact=DerivedFact(
            produced_by_atom="calc.formula", produced_by_atom_version="2.0.0", source_ref="s", concept="GROSS_MARGIN",
            period_ref="6M 2025", formula_id="GROSS_MARGIN", formula_version="v", expression="x", inputs=["a"],
            value=0.25, unit="доля",
        ),
        concept="GROSS_MARGIN", period_ref="6M 2025", definition_origin="registry",
    )
    bad = good.model_copy(update={
        "derived_fact": good.derived_fact.model_copy(update={"period_ref": "6M 2026", "value": 5.0}),
        "period_ref": "6M 2026",
    })
    import finengine.skills.atoms.verify_bounds as vb

    original = vb._bounds_for
    vb._bounds_for = lambda concept, reg: {"min_value": 0.0, "max_value": 1.0}
    try:
        out = verify_bounds_gate(NodeArgs(concept="GROSS_MARGIN"), _ctx_for_gates([good, bad]))
    finally:
        vb._bounds_for = original

    kept = [e for e in out if isinstance(e, CalculatedMetric)]
    assert [m.period_ref for m in kept] == ["6M 2025"], "нарушившее границу значение отброшено, остальные остались"
    assert any(getattr(e, "status", "") == "fail" for e in out)


def test_identity_and_cross_pair_each_calculated_value_with_the_extracted_one_of_its_own_period():
    def _calc(period: str, value: float) -> CalculatedMetric:
        return CalculatedMetric(
            derived_fact=DerivedFact(
                produced_by_atom="calc.formula", produced_by_atom_version="2.0.0", source_ref=f"s-{period}",
                concept="REVENUE", period_ref=period, formula_id="REVENUE", formula_version="v",
                expression="x", inputs=["a"], value=value, unit="млн. руб.",
            ),
            concept="REVENUE", period_ref=period, definition_origin="registry",
        )

    extracted = [_extracted("REVENUE", 100.0, "6M 2025"), _extracted("REVENUE", 200.0, "6M 2026")]
    calcs = [_calc("6M 2025", 100.0), _calc("6M 2026", 200.0)]
    for gate in (verify_cross_gate, verify_identity_gate):
        out = gate(NodeArgs(concept="REVENUE"), _ctx_for_gates([*extracted, *calcs]))
        findings = [e for e in out if isinstance(e, VerificationFinding)]
        assert [f.status for f in findings] == ["pass", "pass"], (
            f"{gate.__name__}: значение каждого периода сверяется со своим извлечённым, не с первым"
        )
        assert sorted(m.period_ref for m in out if isinstance(m, CalculatedMetric)) == ["6M 2025", "6M 2026"]
