"""Architecture and runtime guards for the Slice 13.5 production cutover."""
from __future__ import annotations

import inspect
from pathlib import Path

from finengine.contracts import AdHocFormulaSpec, GoalIR
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.atoms.intent import root_from_goal
from finengine.skills.atoms.run_passport import build_run_passport
from finengine.skills.production_runtime import (
    ProductionContext,
    execute_production_goal,
    prepare_production_context,
)
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.store.cases import SqlCaseStore


ROOT = Path(__file__).resolve().parents[2]


def _goal(kind: str, **parameters) -> GoalIR:
    return GoalIR(
        raw_query=f"test {kind}", intent_kind=kind,
        entity_ref="issuer:test", period_ref="p-2025-fy", parameters=parameters,
    )


def test_all_goal_types_resolve_to_semantic_targets_or_explicit_refusal():
    supported = {
        "full_analysis": "ReportOutput",
        "extract_registered_metric": "ExtractedMetric",
        "extract_ad_hoc_metric": "ExtractedMetric",
        "calculate_registered_metric": "CalculatedMetric",
        "calculate_ad_hoc_metric": "CalculatedMetric",
        "monitor_changes": "PeriodChangeReport",
        "form": "FormOutput",
    }
    unsupported = {
        "extract_structure", "document_qa", "compare_metrics", "explain_change",
        "validate_report", "plan_fact_analysis",
    }
    for kind, output_type in supported.items():
        if kind == "calculate_ad_hoc_metric":
            # Срез 27/31: root_from_goal требует доверенную формулу от intent.parse
            # (`goal.parameters["ad_hoc_formula"]`) для этого вида — см.
            # tests/atoms/test_intent_parse.py за той же правкой.
            params = {"ad_hoc_formula": AdHocFormulaSpec(
                term="test", expression="A - B", dependencies=["A", "B"],
            ).model_dump(mode="json")}
        else:
            params = {}
        goal = _goal(kind, **params)
        if "registered_metric" in kind:
            goal = GoalIR(
                raw_query=f"test {kind}", intent_kind=kind,
                entity_ref="issuer:test", period_ref="p-2025-fy",
                targets=[{"concept": "SALES_REVENUE", "output_kind": "scalar"}],
            )
        roots = root_from_goal(goal)
        assert roots and roots[0].root.output_type == output_type

    passport = build_run_passport("IFRS annual report", goal_ir_id="goal-refusal")
    context = ProductionContext(
        runtime=None,  # refusal occurs before any runtime operation
        passport=passport,
        owner_id="owner",
        case_id="case",
        document_id="doc",
        document_version="sha256:test",
    )
    for kind in unsupported:
        outcome = execute_production_goal(context, _goal(kind), document_text="text")
        assert outcome.refusal_event["type"] == "typed_error"
        assert outcome.refusal_event["error"]["code"] == "unsupported_goal_type"


def test_full_analysis_root_is_report_and_has_distinct_publication_gates():
    goal = _goal("full_analysis")
    root = root_from_goal(goal)[0].root
    assert root.output_type == "ReportOutput"
    passport = build_run_passport(
        "МСФО годовой отчет выручка прибыль",
        goal_ir_id=goal.goal_id,
    )
    registry = build_registry(profile_id=passport.classification.profile_id)
    plan = GraphPlanner(
        registry=registry,
        policy=default_policy(passport.classification.profile_id),
    ).plan(root, run_passport=passport)
    names = [node.node_key.atom_name for node in plan.graph.nodes]
    assert "out.report" in names
    assert "verify.citation" in names
    assert names.count("verify.grounding") >= 2
    assert plan.graph.root.atom_name == "verify.citation"


def test_passport_and_effective_registry_are_frozen_once_at_ingress():
    store = SqlCaseStore("sqlite:///:memory:")
    goal = _goal("form")
    context = prepare_production_context(
        owner_id="owner-1",
        case_id="case-1",
        document_id="doc-1",
        document_text="МСФО годовой отчет компании: выручка и чистая прибыль",
        goal_ir=goal,
        store=store,
    )
    # Target is a frozen dataclass; production code binds the version with
    # dataclasses.replace.  Plan through the actual ingress for the assertion.
    from dataclasses import replace

    target = replace(root_from_goal(goal)[0].root, document_version=context.document_version)
    planned = context.runtime.plan(
        owner_id=context.owner_id,
        case_id=context.case_id,
        goal_ir=goal,
        target=target,
        run_passport=context.passport,
    )
    persisted = store.load_plan_run("owner-1", "case-1", planned.record.plan_run_id)
    assert persisted is not None
    assert persisted.run_passport == context.passport
    assert persisted.registry_fingerprint == context.runtime.registry.fingerprint()


def test_station_producers_are_thin_canonical_shells():
    extract = (ROOT / "station-skills/aifa-extract/scripts/extract.py").read_text(encoding="utf-8")
    analyze = (ROOT / "station-skills/aifa-analyze/scripts/analyze.py").read_text(encoding="utf-8")
    metric = (ROOT / "station-skills/aifa-metric/scripts/metric_run.py").read_text(encoding="utf-8")
    launcher = (ROOT / "station-skills/aifa-extract/scripts/extract_run.sh").read_text(encoding="utf-8")
    forbidden = (
        "two_stage_extractor", "skills.analysis.handler", "analysis_result",
        "extract_stream_from_text", "bulk_extractor",
    )
    for token in forbidden:
        assert token not in extract
        assert token not in analyze
        assert token not in metric
    assert "prepare_production_context" in extract and "execute_production_goal" in extract
    assert "prepare_production_context" in analyze and "execute_production_goal" in analyze
    assert "prepare_production_context" in metric and "execute_production_goal" in metric
    assert "parse_intent" in metric, "aifa-metric — единственный canonical shell, вызывающий intent.parse"
    assert "aifa-calc/run.sh" not in launcher


def test_production_ingress_has_no_legacy_fallback_or_rollout_switch():
    source = inspect.getsource(__import__(
        "finengine.skills.production_runtime", fromlist=["production_runtime"]
    ))
    for token in (
        "analysis_result", "extract_stream_from_text", "skills.analysis.handler",
        "FINENGINE_USE_LEGACY", "rollout_percentage", "parallel_traffic",
    ):
        assert token not in source
