"""Calculated dependencies must reach the next formula with their identities."""

from types import SimpleNamespace

import pytest

from finengine.contracts import CalculatedMetric, DerivedFact, ExtractedMetric, ScalarFact
from finengine.skills.atoms import calc_formula as calc
from finengine.skills.atoms.graph_atoms import default_policy
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry, NodeArgs
from finengine.skills.spec.graph_executor import GraphExecutor
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import OutputSpec, Target


@pytest.fixture
def setup(monkeypatch):
    registry = {"extractable": [{"enum": c, "name_ru": c} for c in ("B", "C", "E")],
                "calculated": [("A", "A", "B + C", ["B", "C"]),
                               ("D", "D", "A / E", ["A", "E"]),
                               ("DOUBLE_A", "double", "A * 2", ["A"])], "calculated_meta": {}}
    monkeypatch.setattr(calc, "registry_for_passport", lambda _: registry)
    passport = SimpleNamespace(classification=SimpleNamespace(profile_id="ifrs_bank_ru", company_type="bank",
                                                            standard="ifrs", language="ru"), registry_version="test",
                               entity=SimpleNamespace(entity_id="company"), methodology_version="test")
    return passport


def extracted(concept, value, period="FY 2024"):
    fact = ScalarFact(concept=concept, period_ref=period, value=value, unit="млн руб.",
                      produced_by_atom="read.value", produced_by_atom_version="1", source_ref="doc")
    return ExtractedMetric(concept=concept, period_ref=period, scalar_fact=fact)


def run(concept, deps, passport, period="FY 2024"):
    ctx = SimpleNamespace(run_id="nested", run_passport=passport, dependencies=lambda: deps)
    return calc.calc_formula_executor(NodeArgs(concept=concept, period_ref=period), ctx)


def calculated_a(value=50, period="FY 2024"):
    fact = DerivedFact(concept="A", period_ref=period, value=value, unit="млн руб.",
                       formula_id="A", formula_version="old-version", expression="B + C", inputs=["b", "c"],
                       produced_by_atom="calc.formula", produced_by_atom_version="1", source_ref="A")
    return CalculatedMetric(concept="A", period_ref=period, derived_fact=fact)


@pytest.mark.parametrize("period", ["FY 2024", None])
def test_second_calculation_uses_saved_value_and_links_to_it(setup, period):
    # The saved A is deliberately different from what absent B/C might yield.
    a, e = calculated_a(70), extracted("E", 10)
    results = [v for v in run("D", [a, e], setup, period) if isinstance(v, CalculatedMetric)]
    assert len(results) == 1
    assert results[0].derived_fact.value == pytest.approx(7)
    assert set(results[0].derived_fact.inputs) == {a.derived_fact.evidence_id, e.scalar_fact.evidence_id}


def test_formula_with_only_calculated_inputs(setup):
    a = calculated_a()
    results = [v for v in run("DOUBLE_A", [a], setup) if isinstance(v, CalculatedMetric)]
    assert len(results) == 1 and results[0].derived_fact.value == pytest.approx(100)
    assert results[0].derived_fact.inputs == [a.derived_fact.evidence_id]
    assert results[0].derived_fact.unit == a.derived_fact.unit


def test_average_uses_both_calculated_balance_dates(setup, monkeypatch):
    registry = {"extractable": [{"enum": "NET_INCOME"}],
                "calculated": [("TOTAL_DEBT", "debt", "B + C", ["B", "C"]),
                               ("D", "D", "NET_INCOME / AVG(TOTAL_DEBT)", ["NET_INCOME", "TOTAL_DEBT"])],
                "calculated_meta": {}}
    monkeypatch.setattr(calc, "registry_for_passport", lambda _: registry)
    balances = []
    for period, value in (("31.12.2023", 40), ("31.12.2024", 60)):
        base = calculated_a(value, period)
        balances.append(base.model_copy(update={"concept": "TOTAL_DEBT", "derived_fact":
                         base.derived_fact.model_copy(update={"concept": "TOTAL_DEBT"})}))
    profit = extracted("NET_INCOME", 10)
    results = [v for v in run("D", [profit, *balances], setup) if isinstance(v, CalculatedMetric)]
    assert len(results) == 1 and results[0].derived_fact.value == pytest.approx(0.2)
    assert set(results[0].derived_fact.inputs) == {profit.scalar_fact.evidence_id,
                                                *(b.derived_fact.evidence_id for b in balances)}


def test_wrong_period_is_not_used(setup):
    results = run("D", [calculated_a(period="FY 2023"), extracted("E", 10)], setup)
    assert not any(isinstance(v, CalculatedMetric) for v in results)
    assert any("A за FY 2024" in v.detail for v in results)


def test_real_graph_executes_two_calculations(setup):
    facts = [extracted(c, v) for c, v in (("B", 20), ("C", 30), ("E", 10))]
    common = dict(version="1", sandbox_profile="compute_no_network", execution_kind="deterministic",
                  cache_policy="none", cost_class="free")
    registry = AtomRegistry([
        AtomDef(name="calc.formula", produces=tuple(OutputSpec("CalculatedMetric", c) for c in ("A", "D")),
                dynamic_requires=calc.calc_formula_requires, executor=calc.calc_formula_executor, **common),
        AtomDef(name="read.value", produces=tuple(OutputSpec("ExtractedMetric", c) for c in ("B", "C", "E")),
                executor=lambda args, ctx: [f for f in facts if f.concept == args.concept], **common),
    ])
    plan, args = GraphPlanner(registry=registry, policy=default_policy()).plan_ex(
        Target("CalculatedMetric", concept_id="D", period_ref="FY 2024"), run_passport=setup)
    assert plan.feasibility.feasible
    result = GraphExecutor(registry=registry).run(plan.graph, args, run_id="nested", run_passport=setup)
    values = {v.concept: v for group in result.evidence_by_key.values() for v in group if isinstance(v, CalculatedMetric)}
    assert values["A"].derived_fact.value == pytest.approx(50)
    assert values["D"].derived_fact.value == pytest.approx(5)
    assert values["A"].derived_fact.evidence_id in values["D"].derived_fact.inputs
