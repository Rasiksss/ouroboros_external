"""Slice 10 acceptance checks against real SQLite persistence.

SQLite is intentionally a local/test backend only.  These tests exercise the
same SQL Case/PlanRun/override contract across fresh store instances without
pretending to prove PostgreSQL ``FOR UPDATE`` semantics.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import inspect

from finengine.contracts import (
    AdHocConceptSpec,
    AdHocFormulaSpec,
    CalculatedMetric,
    CorrectionIntent,
    DerivedFact,
    ExtractedMetric,
    GoalIR,
    NodeKey,
    OverrideChoicePoint,
    PlanPolicy,
    ScalarFact,
    UserProvidedMetric,
)
from finengine.skills.atoms.read_override import (
    ATOM_VERSION as OVERRIDE_VERSION,
    metric_evidence_id,
    metric_value_unit_period,
    read_override_executor,
    read_override_requires,
)
from finengine.skills.case_runtime import CaseRuntime
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry
from finengine.skills.spec.graph_executor import ExecutionBudget
from finengine.skills.spec.target import InputSpec, OutputSpec, Target
from finengine.store.cases import CaseConflictError, SqlCaseStore, StalePlanRunError


@dataclass
class Calls:
    read: int = 0
    calc: int = 0
    fail_calc_once: bool = False


def _registry(calls: Calls) -> AtomRegistry:
    def read_sales(args, ctx):  # noqa: ARG001
        calls.read += 1
        fact = ScalarFact(
            produced_by_atom="read.test",
            produced_by_atom_version="1.0.0",
            source_ref="document:test",
            document_version=args.document_version,
            concept=args.concept,
            period_ref=args.period_ref or "unknown",
            basis=args.basis,
            value=100.0,
            unit="RUB",
        )
        return [ExtractedMetric(
            scalar_fact=fact,
            concept=args.concept or "SALES",
            period_ref=args.period_ref or "unknown",
        )]

    def calc_requires(args, run_passport):  # noqa: ARG001
        return (InputSpec(input_type="MetricEvidence", concept="SALES"),)

    def calc_margin(args, ctx):
        calls.calc += 1
        if calls.fail_calc_once:
            calls.fail_calc_once = False
            raise RuntimeError("synthetic interruption after dependency persisted")
        metric = next(
            value for value in ctx.dependencies()
            if isinstance(value, (ExtractedMetric, CalculatedMetric, UserProvidedMetric))
            and value.concept == "SALES"
        )
        value, unit, _period, _basis, document_version = metric_value_unit_period(metric)
        dependency_id = metric_evidence_id(metric)
        fact = DerivedFact(
            produced_by_atom="calc.test",
            produced_by_atom_version="1.0.0",
            source_ref="formula:test",
            document_version=document_version,
            concept=args.concept,
            period_ref=args.period_ref,
            basis=args.basis,
            formula_id="margin-test",
            formula_version="1.0.0",
            expression="SALES * 2",
            inputs=[dependency_id],
            value=value * 2,
            unit=unit,
        )
        return [CalculatedMetric(
            derived_fact=fact,
            concept=args.concept or "MARGIN",
            period_ref=args.period_ref or "unknown",
        )]

    key_fields = (
        "concept", "entity_ref", "period_ref", "basis",
        "document_version", "policy_version",
    )
    return AtomRegistry([
        AtomDef(
            name="goal.join", version="1.0.0", produces=("CompoundResult",),
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="run_scoped", cost_class="free", executor=lambda *_: [],
            key_fields=("scenario_refs",),
        ),
        AtomDef(
            name="read.test", version="1.0.0",
            produces=(OutputSpec("ExtractedMetric", "SALES"),),
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="run_scoped", cost_class="cheap", executor=read_sales,
            key_fields=key_fields,
        ),
        AtomDef(
            name="calc.test", version="1.0.0",
            produces=(OutputSpec("CalculatedMetric", "MARGIN"),),
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="run_scoped", cost_class="cheap", executor=calc_margin,
            dynamic_requires=calc_requires, key_fields=key_fields,
        ),
        AtomDef(
            name="read.override", version=OVERRIDE_VERSION,
            produces=(OutputSpec("UserProvidedMetric"),),
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="none", cost_class="free", executor=read_override_executor,
            dynamic_requires=read_override_requires,
            key_fields=(
                "concept", "entity_ref", "period_ref", "basis", "document_version",
                "policy_version", "overridden_evidence_id",
            ),
        ),
    ])


def _runtime(url: str, calls: Calls) -> CaseRuntime:
    return CaseRuntime(
        store=SqlCaseStore(url), registry=_registry(calls),
        policy=PlanPolicy(policy_version="slice10-test"),
        budget=ExecutionBudget(max_retries=0),
    )


def _db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'slice10.db'}"


def _goal(text: str, kind: str = "extract_registered_metric") -> GoalIR:
    return GoalIR(raw_query=text, intent_kind=kind)


def _target(output_type: str, concept: str) -> Target:
    return Target(
        output_type=output_type, concept_id=concept, entity_ref="ACME",
        period_ref="2025", basis="IFRS",
    )


def _prepare(runtime: CaseRuntime, *, owner: str = "alice", case_id: str = "case-1", version: str = "sha256:v1"):
    runtime.ensure_case(owner_id=owner, case_id=case_id)
    runtime.register_document(
        owner_id=owner, case_id=case_id, logical_name="report.pdf",
        document_id=f"doc:{version}", document_version=version,
    )


def _extract_sales(runtime: CaseRuntime, *, owner: str = "alice", case_id: str = "case-1"):
    planned = runtime.plan(
        owner_id=owner, case_id=case_id, goal_ir=_goal("sales"),
        target=_target("ExtractedMetric", "SALES"),
    )
    result = runtime.execute(owner_id=owner, case_id=case_id, plan_run_id=planned.record.plan_run_id)
    assert result.succeeded
    metric = next(v for v in result.evidence_by_key[result.node_runs.keys().__iter__().__next__()] if isinstance(v, ExtractedMetric))
    return planned, metric


def test_compound_plan_is_persisted_executed_and_streamed_as_one_dag(tmp_path: Path):
    runtime = _runtime(_db_url(tmp_path), Calls())
    _prepare(runtime)
    goal = GoalIR(raw_query="extract sales and calculate margin", intent_kind="extract_registered_metric")
    planned = runtime.plan_many(
        owner_id="alice", case_id="case-1", goal_ir=goal,
        requests=[
            (_target("ExtractedMetric", "SALES"), {}, "sales", "extract_registered_metric", ()),
            (_target("CalculatedMetric", "MARGIN"), {}, "margin", "calculate_registered_metric", ("sales",)),
        ],
    )
    events: list[dict] = []
    result = runtime.execute(
        owner_id="alice", case_id="case-1", plan_run_id=planned.record.plan_run_id,
        event_sink=events.append,
    )

    assert result.succeeded
    assert [scenario.goal_ref for scenario in planned.record.graph.scenarios] == ["sales", "margin"]
    graph_event = next(event for event in events if event["type"] == "plan_graph")
    assert graph_event["scenarios"][1]["depends_on"] == ["sales"]
    assert any(node["scenario_refs"] == ["sales", "margin"] for node in graph_event["nodes"])


def _root_payload(result):
    return result.evidence_by_key[result.node_runs.keys().__iter__().__next__()]


def test_schema_and_instance_a_to_b_reuse_delta_plan(tmp_path):
    url = _db_url(tmp_path)
    calls_a = Calls()
    a = _runtime(url, calls_a)
    _prepare(a)

    first = a.plan(
        owner_id="alice", case_id="case-1", goal_ir=_goal("sales"),
        target=_target("ExtractedMetric", "SALES"),
    )
    assert a.execute(owner_id="alice", case_id="case-1", plan_run_id=first.record.plan_run_id).succeeded
    assert calls_a.read == 1

    tables = set(inspect(a.store._engine).get_table_names())  # noqa: SLF001 — schema acceptance check
    assert {
        "analysis_case", "analysis_case_document", "analysis_case_artifact",
        "analysis_plan_run", "analysis_plan_node_run", "analysis_override_event",
    } <= tables

    # Fresh store/runtime = different service instance using only persistent DB state.
    calls_b = Calls()
    b = _runtime(url, calls_b)
    repeated = b.plan(
        owner_id="alice", case_id="case-1", goal_ir=_goal("sales again"),
        target=_target("ExtractedMetric", "SALES"),
    )
    assert len(repeated.record.available_leaf_keys) == 1
    assert repeated.record.available_leaf_keys[0]["output_type"] == "ExtractedMetric"
    assert b.execute(owner_id="alice", case_id="case-1", plan_run_id=repeated.record.plan_run_id).succeeded
    assert calls_b.read == 0

    delta = b.plan(
        owner_id="alice", case_id="case-1",
        goal_ir=_goal("calculate margin", "calculate_registered_metric"),
        target=_target("CalculatedMetric", "MARGIN"),
    )
    assert any(x["output_type"] == "MetricEvidence" for x in delta.record.available_leaf_keys)
    result = b.execute(owner_id="alice", case_id="case-1", plan_run_id=delta.record.plan_run_id)
    assert result.succeeded
    assert calls_b.read == 0
    assert calls_b.calc == 1
    root = result.evidence_by_key[delta.record.graph.root]
    assert isinstance(root[0], CalculatedMetric)
    assert root[0].derived_fact.value == 200.0


def test_owner_isolation_same_case_id(tmp_path):
    url = _db_url(tmp_path)
    alice_calls = Calls()
    alice = _runtime(url, alice_calls)
    _prepare(alice, owner="alice", case_id="shared-name")
    planned = alice.plan(
        owner_id="alice", case_id="shared-name", goal_ir=_goal("sales"),
        target=_target("ExtractedMetric", "SALES"),
    )
    assert alice.execute(owner_id="alice", case_id="shared-name", plan_run_id=planned.record.plan_run_id).succeeded

    bob_calls = Calls()
    bob = _runtime(url, bob_calls)
    _prepare(bob, owner="bob", case_id="shared-name")
    bob_plan = bob.plan(
        owner_id="bob", case_id="shared-name", goal_ir=_goal("sales"),
        target=_target("ExtractedMetric", "SALES"),
    )
    assert bob_plan.record.available_leaf_keys == []
    assert bob.execute(owner_id="bob", case_id="shared-name", plan_run_id=bob_plan.record.plan_run_id).succeeded
    assert bob_calls.read == 1
    assert bob.store.list_artifacts("bob", "shared-name")
    assert alice.store.list_artifacts("alice", "shared-name")


def test_same_filename_new_hash_invalidates_only_dependent_graph_and_stales_old_leaf_plan(tmp_path):
    url = _db_url(tmp_path)
    calls = Calls()
    runtime = _runtime(url, calls)
    _prepare(runtime)

    sales_plan = runtime.plan(
        owner_id="alice", case_id="case-1", goal_ir=_goal("sales"),
        target=_target("ExtractedMetric", "SALES"),
    )
    assert runtime.execute(owner_id="alice", case_id="case-1", plan_run_id=sales_plan.record.plan_run_id).succeeded
    margin_plan = runtime.plan(
        owner_id="alice", case_id="case-1", goal_ir=_goal("margin", "calculate_registered_metric"),
        target=_target("CalculatedMetric", "MARGIN"),
    )
    assert runtime.execute(owner_id="alice", case_id="case-1", plan_run_id=margin_plan.record.plan_run_id).succeeded

    # Plan that explicitly depends on the persisted v1 leaf but has not run yet.
    stale_candidate = runtime.plan(
        owner_id="alice", case_id="case-1", goal_ir=_goal("margin later", "calculate_registered_metric"),
        target=_target("CalculatedMetric", "MARGIN"),
    )
    assert stale_candidate.record.available_leaf_keys

    independent = ExtractedMetric(
        scalar_fact=ScalarFact(
            produced_by_atom="manual.independent", produced_by_atom_version="1",
            source_ref="manual", concept="INDEPENDENT", period_ref="2025",
            value=7.0, unit="RUB",
        ),
        concept="INDEPENDENT", period_ref="2025",
    )
    independent_id = runtime.store.put_artifact(
        "alice", "case-1",
        node_key=NodeKey(atom_name="manual.independent", atom_version="1", args_fingerprint="concept=INDEPENDENT"),
        output_type="ExtractedMetric", payloads=[independent], concept="INDEPENDENT",
        entity_ref="ACME", period_ref="2025", basis="IFRS", document_version=None,
        policy_version="slice10-test",
    )

    _snap, invalidated = runtime.register_document(
        owner_id="alice", case_id="case-1", logical_name="report.pdf",
        document_id="doc:v2", document_version="sha256:v2",
    )
    assert invalidated
    rows = {r["artifact_id"]: r for r in runtime.store.list_artifacts("alice", "case-1", valid_only=False)}
    assert rows[independent_id]["valid"] is True
    assert any(r["document_version"] == "sha256:v1" and not r["valid"] for r in rows.values())

    with pytest.raises(StalePlanRunError):
        runtime.execute(owner_id="alice", case_id="case-1", plan_run_id=stale_candidate.record.plan_run_id)

    fresh = runtime.plan(
        owner_id="alice", case_id="case-1", goal_ir=_goal("sales on replacement"),
        target=_target("ExtractedMetric", "SALES"),
    )
    assert fresh.record.target.document_version == "sha256:v2"
    before = calls.read
    assert runtime.execute(owner_id="alice", case_id="case-1", plan_run_id=fresh.record.plan_run_id).succeeded
    assert calls.read == before + 1


def test_restart_resume_planrun_keeps_succeeded_dependency_and_increments_attempt(tmp_path):
    url = _db_url(tmp_path)
    calls_a = Calls(fail_calc_once=True)
    a = _runtime(url, calls_a)
    _prepare(a)
    planned = a.plan(
        owner_id="alice", case_id="case-1", goal_ir=_goal("margin", "calculate_registered_metric"),
        target=_target("CalculatedMetric", "MARGIN"),
    )
    first = a.execute(owner_id="alice", case_id="case-1", plan_run_id=planned.record.plan_run_id)
    assert not first.succeeded
    read_key = next(k for k in first.node_runs if k.atom_name == "read.test")
    calc_key = next(k for k in first.node_runs if k.atom_name == "calc.test")
    assert first.node_runs[read_key].status == "succeeded"
    failed_attempt = first.node_runs[calc_key].attempt
    assert calls_a.read == 1

    calls_b = Calls()
    b = _runtime(url, calls_b)
    resumed = b.execute(owner_id="alice", case_id="case-1", plan_run_id=planned.record.plan_run_id)
    assert resumed.succeeded
    assert calls_b.read == 0
    assert calls_b.calc == 1
    assert resumed.node_runs[calc_key].attempt == failed_attempt + 1


def test_optimistic_concurrent_update_allows_one_writer(tmp_path):
    url = _db_url(tmp_path)
    a = SqlCaseStore(url)
    b = SqlCaseStore(url)
    snap = a.ensure_case(__import__("finengine.contracts", fromlist=["CaseIdentity"]).CaseIdentity(
        case_id="case-1", owner_id="alice",
    ))

    def write(store, text):
        try:
            return store.update_case_head(
                "alice", "case-1", goal_ir=_goal(text), expected_revision=snap.revision,
            ).revision
        except CaseConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: write(*item), [(a, "one"), (b, "two")]))
    assert sum(isinstance(x, int) for x in results) == 1
    assert sum(isinstance(x, CaseConflictError) for x in results) == 1
    assert a.load_case("alice", "case-1").revision == 1


def _sales_evidence(runtime: CaseRuntime) -> ExtractedMetric:
    rows = runtime.store.list_artifacts("alice", "case-1")
    return next(
        value for row in rows for value in row["payloads"]
        if isinstance(value, ExtractedMetric) and value.concept == "SALES"
    )


def test_override_cancel_reoverride_is_audited_and_changes_semantic_selection(tmp_path):
    url = _db_url(tmp_path)
    calls = Calls()
    runtime = _runtime(url, calls)
    _prepare(runtime)
    plan = runtime.plan(
        owner_id="alice", case_id="case-1", goal_ir=_goal("sales"),
        target=_target("ExtractedMetric", "SALES"),
    )
    assert runtime.execute(owner_id="alice", case_id="case-1", plan_run_id=plan.record.plan_run_id).succeeded
    original = _sales_evidence(runtime)
    original_id = original.scalar_fact.evidence_id

    apply_120 = CorrectionIntent(
        action="apply", evidence_id=original_id, concept="SALES", value=120,
        unit="RUB", period_ref="2025", basis="IFRS",
        reason="user supplied corrected filing value",
        original_wording="Нет, продажи за 2025 — 120 RUB",
    )
    wrapper = runtime.apply_correction(
        owner_id="alice", case_id="case-1", correction=apply_120, idempotency_key="cmd-1",
    )
    assert isinstance(wrapper, UserProvidedMetric)
    assert wrapper.human_override.supersedes == original_id
    assert runtime.store.find_evidence(
        "alice", "case-1", wrapper.human_override.evidence_id, valid_only=True,
    ) == [wrapper]
    assert _sales_evidence(runtime).scalar_fact.value == 100.0  # source evidence is immutable

    # Replay is idempotent even though the Case revision has advanced.
    same = runtime.apply_correction(
        owner_id="alice", case_id="case-1", correction=apply_120, idempotency_key="cmd-1",
        expected_revision=0,
    )
    assert isinstance(same, UserProvidedMetric)
    assert same.human_override.evidence_id == wrapper.human_override.evidence_id
    assert len(runtime.store.override_history("alice", "case-1")) == 1

    margin_120 = runtime.plan(
        owner_id="alice", case_id="case-1", goal_ir=_goal("margin", "calculate_registered_metric"),
        target=_target("CalculatedMetric", "MARGIN"),
    )
    out = runtime.execute(owner_id="alice", case_id="case-1", plan_run_id=margin_120.record.plan_run_id)
    assert out.evidence_by_key[margin_120.record.graph.root][0].derived_fact.value == 240.0

    cancel = CorrectionIntent(
        action="cancel", evidence_id=wrapper.human_override.evidence_id, concept="SALES",
        original_wording="Отмени мою поправку",
    )
    runtime.cancel_correction(
        owner_id="alice", case_id="case-1", correction=cancel, idempotency_key="cmd-2",
    )
    assert runtime.store.active_overrides("alice", "case-1") == {}
    assert runtime.store.find_evidence(
        "alice", "case-1", wrapper.human_override.evidence_id, valid_only=True,
    ) == []
    assert runtime.store.find_evidence(
        "alice", "case-1", wrapper.human_override.evidence_id,
    ) == [wrapper]

    margin_100 = runtime.plan(
        owner_id="alice", case_id="case-1", goal_ir=_goal("margin again", "calculate_registered_metric"),
        target=_target("CalculatedMetric", "MARGIN"),
    )
    out = runtime.execute(owner_id="alice", case_id="case-1", plan_run_id=margin_100.record.plan_run_id)
    assert out.evidence_by_key[margin_100.record.graph.root][0].derived_fact.value == 200.0

    apply_130 = apply_120.model_copy(update={
        "value": 130.0, "original_wording": "Уточнение: продажи 130 RUB",
    })
    second = runtime.apply_correction(
        owner_id="alice", case_id="case-1", correction=apply_130, idempotency_key="cmd-3",
    )
    assert isinstance(second, UserProvidedMetric)
    history = runtime.store.override_history("alice", "case-1")
    assert [event.action for event, _ in history] == ["apply", "cancel", "apply"]
    assert runtime.store.active_overrides("alice", "case-1")[original_id].human_override.numeric_value == 130.0


def test_ambiguous_wrong_dimensions_and_cross_owner_override_are_rejected(tmp_path):
    url = _db_url(tmp_path)
    runtime = _runtime(url, Calls())
    _prepare(runtime)
    plan = runtime.plan(
        owner_id="alice", case_id="case-1", goal_ir=_goal("sales"),
        target=_target("ExtractedMetric", "SALES"),
    )
    runtime.execute(owner_id="alice", case_id="case-1", plan_run_id=plan.record.plan_run_id)
    original = _sales_evidence(runtime)

    # A second same-concept fact in another period makes concept-only correction ambiguous.
    second_fact = ExtractedMetric(
        scalar_fact=ScalarFact(
            produced_by_atom="read.test", produced_by_atom_version="1.0.0",
            source_ref="document:test", document_version="sha256:v1",
            concept="SALES", period_ref="2024", basis="IFRS", value=90, unit="RUB",
        ),
        concept="SALES", period_ref="2024",
    )
    runtime.store.put_artifact(
        "alice", "case-1",
        node_key=NodeKey(atom_name="read.test", atom_version="1.0.0", args_fingerprint="manual-2024"),
        output_type="ExtractedMetric", payloads=[second_fact], concept="SALES", entity_ref="ACME",
        period_ref="2024", basis="IFRS", document_version="sha256:v1", policy_version="slice10-test",
    )
    ambiguous = runtime.apply_correction(
        owner_id="alice", case_id="case-1",
        correction=CorrectionIntent(
            action="apply", concept="SALES", value=111,
            original_wording="Продажи должны быть 111",
        ),
        idempotency_key="ambiguous",
    )
    assert isinstance(ambiguous, OverrideChoicePoint)
    assert len(ambiguous.candidate_evidence_ids) == 2

    for updates, expected in [
        ({"unit": "USD"}, "unit mismatch"),
        ({"period_ref": "2023"}, "period mismatch"),
        ({"basis": "RAS"}, "basis mismatch"),
    ]:
        correction = CorrectionIntent(
            action="apply", evidence_id=original.scalar_fact.evidence_id,
            concept="SALES", value=111, original_wording="точная поправка", **updates,
        )
        with pytest.raises(ValueError, match=expected):
            runtime.apply_correction(
                owner_id="alice", case_id="case-1", correction=correction,
                idempotency_key=f"bad-{expected}",
            )

    bob = _runtime(url, Calls())
    _prepare(bob, owner="bob", case_id="case-1")
    with pytest.raises(KeyError):
        bob.apply_correction(
            owner_id="bob", case_id="case-1",
            correction=CorrectionIntent(
                action="apply", evidence_id=original.scalar_fact.evidence_id,
                concept="SALES", value=777, original_wording="чужая поправка",
            ),
            idempotency_key="cross-owner",
        )
    assert bob.store.active_overrides("bob", "case-1") == {}


def test_ad_hoc_specs_are_case_bound_owner_isolated_and_idempotent(tmp_path):
    store = SqlCaseStore(_db_url(tmp_path))
    from finengine.contracts import CaseIdentity

    store.ensure_case(CaseIdentity(case_id="case-1", owner_id="alice"))
    store.ensure_case(CaseIdentity(case_id="case-1", owner_id="bob"))
    concept = AdHocConceptSpec(term="торговая площадь")
    formula = AdHocFormulaSpec(
        term="выручка на площадь", expression="SALES / AREA",
        dependencies=["SALES", "AREA"],
    )

    store.put_ad_hoc_spec("alice", "case-1", concept)
    store.put_ad_hoc_spec("alice", "case-1", formula)
    # Same spec id is an idempotent replacement, not another definition.
    store.put_ad_hoc_spec("alice", "case-1", formula)

    alice = store.load_case("alice", "case-1")
    bob = store.load_case("bob", "case-1")
    assert alice.ad_hoc_concepts == {concept.spec_id: concept}
    assert alice.ad_hoc_formulas == {formula.spec_id: formula}
    assert bob.ad_hoc_concepts == {}
    assert bob.ad_hoc_formulas == {}
