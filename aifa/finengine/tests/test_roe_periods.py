"""ROE must retain both balance dates through planning and the legacy adapter."""

from types import SimpleNamespace

import pytest

from finengine.contracts import CalculatedMetric, EvidenceBundleV2, ExtractedMetric, ScalarFact, VerificationFinding
from finengine.skills.atoms import calc_formula as calc
from finengine.skills.evidence_bundle import legacy_rows_from_bundle
from finengine.skills.metric_schema import registry_for_profile
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry, NodeArgs
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import OutputSpec, Target
from finengine.skills.atoms.graph_atoms import default_policy


@pytest.fixture
def inputs(monkeypatch):
    registry = registry_for_profile("ifrs_bank_ru")
    monkeypatch.setattr(calc, "registry_for_passport", lambda _: registry)
    facts = [ScalarFact(produced_by_atom="read.value", produced_by_atom_version="1", source_ref="doc",
                        concept=concept, period_ref=period, value=value, unit="млн руб.")
             for concept, period, value in [("NET_INCOME", "FY 2024", 1580300),
                                            ("TOTAL_EQUITY", "31.12.2024", 7173500),
                                            ("TOTAL_EQUITY", "31.12.2023", 6584400)]]
    passport = SimpleNamespace(classification=SimpleNamespace(profile_id="ifrs_bank_ru", company_type="bank",
                                                            standard="ifrs", language="ru"), registry_version="test",
                               entity=SimpleNamespace(entity_id="bank"), methodology_version="test")
    return facts, passport


def execute(facts, passport, period):
    deps = [ExtractedMetric(scalar_fact=f, concept=f.concept, period_ref=f.period_ref) for f in facts]
    ctx = SimpleNamespace(run_passport=passport, run_id="roe-test", dependencies=lambda: deps)
    return calc.calc_formula_executor(NodeArgs(concept="ROE", period_ref=period), ctx)


def test_adapter_keeps_every_original_period(inputs):
    facts, _ = inputs
    rows = legacy_rows_from_bundle(EvidenceBundleV2(run_id="test", scalar_facts=facts), main_period="FY 2024")
    equity = next(row for row in rows["metrics"] if row["enum"] == "TOTAL_EQUITY")
    assert {item["period"] for item in equity["period_values"]} == {"31.12.2023", "31.12.2024"}


@pytest.mark.parametrize("period", [None, "FY 2024", "2024"])
def test_roe_calculates_from_both_dates(inputs, period):
    facts, passport = inputs
    results = [r for r in execute(facts, passport, period) if isinstance(r, CalculatedMetric)]
    result = next(r for r in results if r.period_ref == "FY 2024")
    assert result.derived_fact.value == pytest.approx(22.972982795339405)
    assert set(result.derived_fact.inputs) == {f.evidence_id for f in facts}
    if period is None:
        assert not any(isinstance(r, VerificationFinding) for r in execute(facts, passport, period))


def test_missing_average_date_is_explained(inputs):
    facts, passport = inputs
    results = execute(facts[:2], passport, "FY 2024")
    assert not any(isinstance(r, CalculatedMetric) for r in results)
    assert any("TOTAL_EQUITY" in r.detail and "31.12.2023" in r.detail for r in results)


def test_planner_requests_both_capital_dates(inputs):
    from finengine.skills.spec.graph_executor import GraphExecutor
    facts, passport = inputs
    calls = []

    def read(args, ctx):
        calls.append((args.concept, args.period_ref))
        return [ExtractedMetric(scalar_fact=f, concept=f.concept, period_ref=f.period_ref) for f in facts
                if f.concept == args.concept and f.period_ref == args.period_ref]

    common = dict(version="1", sandbox_profile="compute_no_network", execution_kind="deterministic",
                  cache_policy="none", cost_class="free")
    registry = AtomRegistry([
        AtomDef(name="calc.formula", produces=(OutputSpec("CalculatedMetric", "ROE"),),
                dynamic_requires=calc.calc_formula_requires, executor=calc.calc_formula_executor, **common),
        AtomDef(name="read.value", produces=(OutputSpec("ExtractedMetric", "NET_INCOME"),
                                              OutputSpec("ExtractedMetric", "TOTAL_EQUITY")), executor=read, **common),
    ])
    result, args = GraphPlanner(registry=registry, policy=default_policy()).plan_ex(
        Target("CalculatedMetric", concept_id="ROE", period_ref="FY 2024"), run_passport=passport)
    assert result.feasibility.feasible
    assert {a.period_ref for a in args.values() if a.concept == "TOTAL_EQUITY"} == {"31.12.2023", "31.12.2024"}
    execution = GraphExecutor(registry=registry).run(result.graph, args, run_id="graph-roe", run_passport=passport)
    values = [v for group in execution.evidence_by_key.values() for v in group if isinstance(v, CalculatedMetric)]
    assert values and values[0].derived_fact.value == pytest.approx(22.972982795339405)
    assert set(calls) == {("NET_INCOME", "FY 2024"), ("TOTAL_EQUITY", "31.12.2023"), ("TOTAL_EQUITY", "31.12.2024")}


def test_other_dates_do_not_change_requested_roe_or_provenance(inputs):
    facts, passport = inputs
    unrelated = facts[-1].model_copy(update={"period_ref": "30.06.2024", "value": 1e9, "evidence_id": "unrelated"})
    values = [r for r in execute([*facts, unrelated], passport, "FY 2024") if isinstance(r, CalculatedMetric)]
    assert len(values) == 1
    assert values[0].derived_fact.value == pytest.approx(22.972982795339405)
    assert "unrelated" not in values[0].derived_fact.inputs


def test_half_year_average_requires_start_and_end_not_latest_two_dates(inputs):
    _, passport = inputs
    specs = calc.calc_formula_requires(NodeArgs(concept="ROE", period_ref="6M 2024"), passport)
    assert {s.period_ref for s in specs if s.concept == "TOTAL_EQUITY"} == {"31.12.2023", "30.06.2024"}


def test_formula_without_average_still_uses_matching_flow_and_point_periods(inputs, monkeypatch):
    facts, passport = inputs
    registry = registry_for_profile("ifrs_bank_ru")
    registry["calculated"] = [("ROE", "test", "NET_INCOME / TOTAL_EQUITY * 100", ["NET_INCOME", "TOTAL_EQUITY"])]
    monkeypatch.setattr(calc, "registry_for_passport", lambda _: registry)
    results = [r for r in execute(facts, passport, "FY 2024") if isinstance(r, CalculatedMetric)]
    assert len(results) == 1
    assert results[0].derived_fact.value == pytest.approx(1580300 / 7173500 * 100)
    specs = calc.calc_formula_requires(NodeArgs(concept="ROE", period_ref="FY 2024"), passport)
    assert all(s.period_ref is None for s in specs)


def test_missing_flow_period_is_named(inputs):
    facts, passport = inputs
    facts[0] = facts[0].model_copy(update={"period_ref": "FY 2023"})
    results = execute(facts, passport, "FY 2024")
    assert not any(isinstance(r, CalculatedMetric) for r in results)
    assert any("NET_INCOME за FY 2024" in r.detail for r in results)


def test_calculation_refusal_does_not_claim_metric_was_searched():
    from finengine.skills.production_runtime import _incomplete_refusal
    goal = SimpleNamespace(intent_kind="calculate_registered_metric", targets=[SimpleNamespace(concept="ROE")])
    outcome = SimpleNamespace(roots=[])
    reply = _incomplete_refusal(goal, [], outcome, "CalculatedMetric")
    assert reply["error"]["code"] == "calculation_incomplete"
    assert "не найден в отчёте" not in reply["error"]["message"]


def test_refusal_preserves_missing_period_detail_even_if_gate_is_blocked(inputs):
    from finengine.skills.production_runtime import _incomplete_refusal
    facts, passport = inputs
    findings = execute(facts[:2], passport, "FY 2024")
    outcome = SimpleNamespace(roots=[SimpleNamespace(execution=SimpleNamespace(
        errors=[SimpleNamespace(status="blocked", node_key=SimpleNamespace(atom_name="verify.bounds"), error="no result")],
        evidence_by_key={"calc": findings}))])
    goal = SimpleNamespace(intent_kind="calculate_registered_metric", targets=[SimpleNamespace(concept="ROE")])
    reply = _incomplete_refusal(goal, [], outcome, "CalculatedMetric")["error"]
    assert reply["code"] == "calculation_incomplete"
    assert "TOTAL_EQUITY на 31.12.2023" in reply["message"]
