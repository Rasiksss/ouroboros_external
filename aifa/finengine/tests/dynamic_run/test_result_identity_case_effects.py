"""Regression measurements for the total executor result identity (K10.2)."""
from __future__ import annotations

from finengine.contracts import ExtractedMetric, GoalIR, NarrativeOutput, NodeKey, ScalarFact
from finengine.skills.spec.dynamic_run import Decision, DynamicRun, RunLimits, render_state
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.graph_planner import AtomStep
from finengine.skills.spec.target import Target
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.production_runtime import _dynamic_outcome
from finengine.store.cases import evidence_id_of

from test_ephemeral_in_graph import CASE, ENTITY, OWNER


def _dynamic(base, text: str, *, limits: RunLimits = RunLimits()) -> DynamicRun:
    return DynamicRun(
        runtime=base,
        owner_id=OWNER,
        case_id=CASE,
        goal=GoalIR(raw_query="Найди EBITDA", intent_kind="document_qa"),
        target=Target(output_type="CandidateLocator", entity_ref=ENTITY),
        inputs=RunInputs(document_text=text, role="other"),
        limits=limits,
    )


def _locate_move() -> Decision:
    return Decision(
        objective="Найти EBITDA",
        reason="Нужен адрес показателя",
        action="execute",
        raw_steps=({"atom": "locate", "concept": "EBITDA"},),
    )


def _metric() -> ExtractedMetric:
    return ExtractedMetric(
        metric_id="metric:revenue",
        concept="SALES_REVENUE",
        period_ref="2024",
        scalar_fact=ScalarFact(
            evidence_id="evidence:revenue",
            produced_by_atom="read.value",
            produced_by_atom_version="1.0.0",
            source_ref="report.pdf:p1",
            concept="SALES_REVENUE",
            period_ref="2024",
            value=100.0,
            unit="RUB_MLN",
        ),
        definition_origin="registry",
    )


def _put(base, output_type: str, payload, fingerprint: str) -> None:
    base.store.put_artifact(
        OWNER,
        CASE,
        node_key=NodeKey(atom_name="probe", atom_version="1.0.0", args_fingerprint=fingerprint),
        output_type=output_type,
        payloads=(payload,),
        concept=getattr(payload, "concept", None),
        entity_ref=ENTITY,
        period_ref=getattr(payload, "period_ref", None),
        basis=None,
        document_version="sha256:v1",
        policy_version=base.policy.policy_version,
    )


def test_repeated_service_only_move_has_zero_target_growth_and_stable_case(
    base,
) -> None:
    """Five identical real moves reuse catalog nodes instead of growing Case."""
    run = _dynamic(base, "financial report EBITDA 100")
    run.run_passport = build_run_passport(
        "financial report",
        goal_ir_id=run.goal.goal_id,
        classification=classification_from_cell("ifrs_nf_ru"),
    )
    move = _locate_move()
    target_deltas: list[int] = []
    row_counts: list[int] = []
    prompt_sizes: list[int] = []

    for _ in range(5):
        before = run._artifact_count()
        run._move(move)
        target_deltas.append(run._artifact_count() - before)
        row_counts.append(len(base.store.list_artifacts(OWNER, CASE)))
        prompt_sizes.append(len(render_state(run, "probe")))

    assert target_deltas == [0, 0, 0, 0, 0]
    assert row_counts == [3, 3, 3, 3, 3]
    assert prompt_sizes[1:] == [prompt_sizes[0]] * 4


def test_partial_limit_is_applied_after_non_evidence_results_are_removed(base, severstal_text) -> None:
    """A technical result must not evict a real evidence card from the terminal cap."""
    narrative = NarrativeOutput(run_id="run:service", text="Служебная сводка")
    metric = _metric()
    _put(base, "NarrativeOutput", narrative, "narrative")
    _put(base, "ExtractedMetric", metric, "metric")
    run = _dynamic(base, severstal_text, limits=RunLimits(max_terminal_evidence=1))
    run.initial_evidence_ids = frozenset()

    selected = run._partial_evidence_ids()

    assert run._artifact_count() == 1
    assert selected == (evidence_id_of(metric),)
    result = run._result("stopped", run._stopped_summary(selected), selected)
    assert result.evidence == (metric,)
    assert "SALES_REVENUE" in result.answer and "100" in result.answer
    events: list[dict] = []
    _dynamic_outcome(result, events.append, case_id=CASE, document_id="doc:v1")
    assert [event["result"]["metric_id"] for event in events if event["type"] == "typed_result"] == [
        metric.metric_id,
    ]


def test_same_catalog_graph_resume_reuses_nodes_with_resolvable_result_ids(base, read_value) -> None:
    """A second execution of one PlanRun publishes the persisted NodeRuns unchanged."""
    goal = GoalIR(raw_query="Найди EBITDA", intent_kind="document_qa")
    passport = build_run_passport(
        "financial report",
        goal_ir_id=goal.goal_id,
        classification=classification_from_cell("ifrs_nf_ru"),
    )
    planned = base.plan_steps(
        owner_id=OWNER,
        case_id=CASE,
        goal_ir=goal,
        target=Target(output_type="ExtractedMetric", entity_ref=ENTITY),
        steps=(AtomStep(atom="read.value", output_type="ExtractedMetric", concept="REVENUE"),),
        run_passport=passport,
    )
    inputs = RunInputs(document_text="financial report EBITDA 100", role="other")

    first = base.execute(
        owner_id=OWNER,
        case_id=CASE,
        plan_run_id=planned.record.plan_run_id,
        inputs=inputs,
    )
    counted_read, _ = read_value
    calls_after_first = counted_read.calls
    rows_after_first = len(base.store.list_artifacts(OWNER, CASE))
    second = base.execute(
        owner_id=OWNER,
        case_id=CASE,
        plan_run_id=planned.record.plan_run_id,
        inputs=inputs,
    )

    assert second.node_runs == first.node_runs
    assert second.evidence_by_key == first.evidence_by_key
    assert counted_read.calls == calls_after_first == 1
    assert len(base.store.list_artifacts(OWNER, CASE)) == rows_after_first
    for node_run in second.node_runs.values():
        for result_id in node_run.produced_evidence_ids:
            assert base.store.find_evidence(OWNER, CASE, result_id), (
                node_run.node_key.atom_name,
                result_id,
            )


def test_catalog_node_results_publish_only_resolvable_ids(base) -> None:
    """One real call of every node in the canonical extraction path has an honest result list."""
    goal = GoalIR(raw_query="Найди выручку", intent_kind="document_qa")
    inputs = RunInputs(document_text="financial report revenue 100", role="other")
    classify = base.plan_steps(
        owner_id=OWNER,
        case_id=CASE,
        goal_ir=goal,
        target=Target(output_type="RunPassport", entity_ref=ENTITY),
        steps=(AtomStep(atom="identify.classify", output_type="RunPassport"),),
    )
    first = base.execute(
        owner_id=OWNER,
        case_id=CASE,
        plan_run_id=classify.record.plan_run_id,
        inputs=inputs,
    )
    passport = base.store.load_case(OWNER, CASE).run_passport
    extract = base.plan_steps(
        owner_id=OWNER,
        case_id=CASE,
        goal_ir=goal,
        target=Target(output_type="ExtractedMetric", entity_ref=ENTITY),
        steps=(AtomStep(atom="read.value", output_type="ExtractedMetric", concept="REVENUE"),),
        run_passport=passport,
    )
    second = base.execute(
        owner_id=OWNER,
        case_id=CASE,
        plan_run_id=extract.record.plan_run_id,
        inputs=inputs,
    )
    observed: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}

    for execution in (first, second):
        for key, node_run in execution.node_runs.items():
            payloads = execution.evidence_by_key.get(key, ())
            observed[key.atom_name] = (
                tuple(type(payload).__name__ for payload in payloads),
                tuple(node_run.produced_evidence_ids),
            )
            assert bool(node_run.produced_evidence_ids) is bool(payloads)
            for result_id in node_run.produced_evidence_ids:
                assert base.store.find_evidence(OWNER, CASE, result_id), (key.atom_name, result_id)

    assert observed.keys() == {
        "ingest.document",
        "identify.classify",
        "locate",
        "read.value",
        "norm.unit",
        "norm.time",
        "verify.bounds",
    }
    assert observed["ingest.document"][0] == ("DocumentText",)
    assert observed["identify.classify"][0] == ("RunPassport",)
    assert observed["locate"][0] == ("CandidateLocator",)
    assert observed["read.value"][0] == ("ExtractedMetric",)
    for atom in ("norm.unit", "norm.time", "verify.bounds"):
        assert observed[atom][0] == ("VerificationFinding", "ExtractedMetric")

    missing = base.plan_steps(
        owner_id=OWNER,
        case_id=CASE,
        goal_ir=goal,
        target=Target(output_type="ExtractedMetric", entity_ref=ENTITY),
        steps=(AtomStep(atom="read.value", output_type="ExtractedMetric", concept="NET_DEBT"),),
        run_passport=passport,
    )
    empty = base.execute(
        owner_id=OWNER,
        case_id=CASE,
        plan_run_id=missing.record.plan_run_id,
        inputs=inputs,
    )
    empty_atoms = {"locate", "read.value", "norm.unit", "norm.time", "verify.bounds"}
    assert empty_atoms <= {key.atom_name for key in empty.node_runs}
    for key, node_run in empty.node_runs.items():
        if key.atom_name in empty_atoms:
            assert empty.evidence_by_key[key] == ()
            assert node_run.produced_evidence_ids == []
