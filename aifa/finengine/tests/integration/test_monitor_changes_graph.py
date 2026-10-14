from __future__ import annotations

from finengine.contracts import (
    ClassificationResult,
    ExtractedMetric,
    GoalIR,
    MonitorResult,
    PlanPolicy,
    RunPassport,
    ScalarFact,
    WatchSchedule,
    WatchSubscription,
)
from finengine.skills.atoms.graph_atoms import build_registry
from finengine.skills.atoms.subject_history import subject_key_from_identity
from finengine.skills.case_runtime import CaseRuntime
from finengine.skills.spec.graph_executor import ExecutionBudget
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry
from finengine.skills.spec.target import OutputSpec, Target
from finengine.skills.watch_subscriptions import run_watch_subscription
from finengine.store.cases import SqlCaseStore
from tests.slice11_fixtures import identity, metric, profile, rule


def test_repeated_monitoring_uses_normal_target_resolver_and_planrun(tmp_path):
    store = SqlCaseStore(f"sqlite:///{tmp_path / 'monitor.db'}")
    entity = identity()
    subject_key = subject_key_from_identity(entity)
    passport = RunPassport(
        goal_ir_id="seed-goal",
        classification=ClassificationResult(
            profile_id="ifrs_nf_ru", standard="ifrs", company_type="non_financial",
            language="ru", reason="fixture",
        ),
        entity=entity, registry_version="registry-1", methodology_version="method-1",
        model_version="model-1", prompt_version="prompt-1", sandbox_image_version="sandbox-1",
    )
    runtime = CaseRuntime(
        store=store, registry=build_registry(),
        policy=PlanPolicy(policy_version="monitor-v1"),
        budget=ExecutionBudget(max_retries=0),
    )
    case = runtime.ensure_case(owner_id="1", case_id="case-1")
    store.update_case_head(
        "1", "case-1", run_passport=passport,
        run_passport_document_version="seed-doc", expected_revision=case.revision,
    )
    store.append_subject_profile(profile(
        1, [metric(100, period="2025-Q1")], plan_run_id="source-plan-1",
        thresholds=[rule(2.0, 3.0, passed=True, headroom=1.0, rule_id="r1", evidence_id="e1")],
    ))
    store.append_subject_profile(profile(
        2, [metric(110, period="2025-Q2")], plan_run_id="source-plan-2",
        thresholds=[rule(2.8, 3.0, passed=True, headroom=0.2, rule_id="r2", evidence_id="e2")],
    ))
    subscription = WatchSubscription(
        subject_key=subject_key, owner_id="1", case_id="case-1",
        check_type="snapshot_and_threshold", schedule=WatchSchedule(kind="manual"),
        allowed_scope=["REVENUE", "LEVERAGE"], policy_version="monitor-v1",
        idempotency_key="monitor-1",
    )
    planned, result = run_watch_subscription(runtime, subscription)
    atom_names = {node.node_key.atom_name for node in planned.record.graph.nodes}
    assert {"calc.series", "watch.diff", "watch.approach", "monitor.compose"} <= atom_names
    assert "read.value" not in atom_names and "calc.formula" not in atom_names
    assert result.succeeded
    monitor = next(v for v in result.evidence_by_key[planned.record.graph.root] if isinstance(v, MonitorResult))
    assert monitor.trace.monitor_plan_run_id == planned.record.plan_run_id
    assert monitor.trace.source_plan_run_ids == ["source-plan-1", "source-plan-2"]
    assert store.load_plan_run("1", "case-1", planned.record.plan_run_id).status == "succeeded"
    assert all("recommend" not in s.narrative.lower() for s in monitor.approaches.signals)


def test_the_chat_monitor_goal_no_longer_needs_the_snapshot_history_root():
    """Срез 32: чат и станция сравнивают периоды ОДНОГО отчёта; корень `MonitorResult`
    (история снимков) остаётся только у подписок по расписанию — см. первый тест."""
    from finengine.skills.atoms.intent import root_from_goal

    root = root_from_goal(GoalIR(raw_query="что изменилось?", intent_kind="monitor_changes"))[0]
    assert root.root.output_type == "PeriodChangeReport"
    assert root.root.params.get("document_scope") != "subject_history"


def test_successful_verified_planrun_appends_immutable_subject_profile(tmp_path):
    store = SqlCaseStore(f"sqlite:///{tmp_path / 'auto-history.db'}")
    entity = identity()
    passport = RunPassport(
        goal_ir_id="goal",
        classification=ClassificationResult(
            profile_id="test", standard="ifrs", company_type="non_financial",
            language="en", reason="fixture",
        ),
        entity=entity, registry_version="registry-1", methodology_version="method-1",
        model_version="model-1", prompt_version="prompt-1", sandbox_image_version="sandbox-1",
    )

    def read(args, ctx):  # noqa: ARG001
        fact = ScalarFact(
            produced_by_atom="read.test", produced_by_atom_version="1",
            source_ref=f"source:{args.period_ref}", concept="REVENUE",
            period_ref=args.period_ref, basis=args.basis, value=100, unit="RUB",
        )
        return [ExtractedMetric(scalar_fact=fact, concept="REVENUE", period_ref=args.period_ref)]

    registry = AtomRegistry([AtomDef(
        name="read.test", version="1", produces=(OutputSpec("ExtractedMetric", "REVENUE"),),
        sandbox_profile="compute_no_network", execution_kind="deterministic",
        cache_policy="run_scoped", cost_class="free", executor=read,
        key_fields=("concept", "entity_ref", "period_ref", "basis", "policy_version"),
    )])
    runtime = CaseRuntime(
        store=store, registry=registry, policy=PlanPolicy(policy_version="p1"),
        budget=ExecutionBudget(max_retries=0),
    )
    runtime.ensure_case(owner_id="1", case_id="case-1")
    for period in ("2025-Q1", "2025-Q2"):
        goal = GoalIR(raw_query=period, intent_kind="extract_registered_metric")
        planned = runtime.plan(
            owner_id="1", case_id="case-1", goal_ir=goal, run_passport=passport,
            target=Target(
                output_type="ExtractedMetric", concept_id="REVENUE",
                entity_ref=entity.entity_id, period_ref=period, basis="IFRS",
            ),
        )
        assert runtime.execute(
            owner_id="1", case_id="case-1", plan_run_id=planned.record.plan_run_id,
        ).succeeded
    history = store.list_subject_profiles("1", subject_key_from_identity(entity).value)
    assert [p.version for p in history] == [1, 2]
    assert [p.period_refs for p in history] == [["2025-Q1"], ["2025-Q2"]]
    assert history[1].previous_snapshot_id == history[0].snapshot_id
