"""Сценарий B7 (Срез 6b): `ref.prior → assess.judgment → explain.internal/
external → out.narrative` — проверено КАК ЕДИНОЕ ЦЕЛОЕ через настоящий
`PlanGraph`+`GraphExecutor`, не только изолированными функциями атомов
(`tests/atoms/test_*.py`). Единственные подмены — модельные/сетевые границы
(`locate`/`read.value`/`assess.judgment`/`explain.internal`/`explain.external`/
`out.narrative`), тем же приёмом, что `test_graph_full_analysis.py` уже
использует для `locate`/`read.value` (`tests/integration/conftest.py`).

Критично проверяет то, что юнит-тесты атомов (с ручными `_Ctx`-заглушками) не
могут: что `AtomDef.dynamic_collection_scope`/`mandatory_gates`/`dynamic_
requires` пяти новых атомов реально складываются в ДОСТИЖИМЫЙ `PlanGraph`
через настоящий `GraphPlanner`, а не только через мою собственную,
потенциально ошибочную имитацию `ctx` в юнит-тестах."""
from __future__ import annotations

from finengine.contracts import (
    CalculatedMetric,
    EvidenceBundleV2,
    Explanation,
    ExplanationFactor,
    ExtractedMetric,
    Judgment,
    NarrativeOutput,
    NumericClaimRecord,
)
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.spec.graph_executor import GraphExecutor, RunInputs
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import Target

_FAKE_VALUES = {"SALES_REVENUE": 1_000_000.0, "NET_INCOME": 120_000.0}


def _metric_fact(evidence):
    return evidence.scalar_fact if isinstance(evidence, ExtractedMetric) else evidence.derived_fact


def _fake_judgment_batch(items, ctx):
    out: dict[str, tuple] = {}
    for args in items:
        concept = args.concept or ""
        fact_ev = next(
            (e for e in ctx.dependencies() if isinstance(e, (ExtractedMetric, CalculatedMetric)) and e.concept == concept),
            None,
        )
        if fact_ev is None:
            out[concept] = ()
            continue
        out[concept] = (Judgment(
            concept=concept, verdict=f"{concept}: показатель в пределах нормы",
            supporting_evidence_ids=[_metric_fact(fact_ev).evidence_id], methodology_ref="test-methodology",
        ),)
    return out


def _fake_explanation_batch(items, ctx):
    out: dict[str, tuple] = {}
    for args in items:
        concept = args.concept or ""
        deps = ctx.dependencies()
        fact_ev = next(
            (e for e in deps if isinstance(e, (ExtractedMetric, CalculatedMetric)) and e.concept == concept), None,
        )
        if fact_ev is None:
            out[concept] = ()
            continue
        evidence_id = _metric_fact(fact_ev).evidence_id
        judgment = next((e for e in deps if isinstance(e, Judgment) and e.concept == concept), None)
        out[concept] = (Explanation(
            judgment_id=judgment.judgment_id if judgment else "no-judgment",
            text=f"{concept} не изменился значимо период к периоду.",
            cited_evidence_ids=[evidence_id],
            factors=[ExplanationFactor(text="совпадает с сопоставимым периодом", causal_type="co_movement",
                                        evidence_ids=[evidence_id])],
        ),)
    return out


def _fake_external_executor(args, ctx):  # noqa: ARG001
    return []  # без внешнего контекста — сценарий проверяет внутреннюю цепочку


def _fake_narrative_executor(args, ctx):  # noqa: ARG001
    deps = ctx.dependencies()
    judgments = [e for e in deps if isinstance(e, Judgment)]
    bundle = next((e for e in deps if isinstance(e, EvidenceBundleV2)), None)
    if not judgments or bundle is None or not bundle.scalar_facts:
        return []
    fact = bundle.scalar_facts[0]
    claim = NumericClaimRecord(
        text=f"{fact.concept} составил {fact.value}", kind="financial", value=fact.value,
        period_ref=fact.period_ref, unit=fact.unit, evidence_id=fact.evidence_id,
    )
    cited = sorted({eid for j in judgments for eid in j.supporting_evidence_ids})
    return [NarrativeOutput(run_id=ctx.run_id, text="Финансовое положение стабильно.",
                             cited_evidence_ids=cited, claims=[claim])]


def test_narrative_root_resolves_through_the_real_planner_and_executor(
    severstal_text, make_fake_locate_and_read_value_executors, make_fake_structure_executor,
    make_fake_entities_executor,
):
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors(_FAKE_VALUES)
    registry = build_registry(
        profile_id="ifrs_nf_ru", locate_executor=locate_exec, read_value_executor=read_value_exec,
        structure_executor=make_fake_structure_executor({}), entities_executor=make_fake_entities_executor([]),
        judgment_executor=_fake_judgment_batch, explanation_executor=_fake_explanation_batch,
        external_executor=_fake_external_executor, narrative_executor=_fake_narrative_executor,
    )
    policy = default_policy("ifrs_nf_ru")
    planner = GraphPlanner(registry=registry, policy=policy)
    goal = Target(output_type="NarrativeOutput", entity_ref="severstal", period_ref="p-2024-fy")
    inputs = RunInputs(document_text=severstal_text, role="other")
    executor = GraphExecutor(registry=registry)

    first, first_args = planner.plan_ex(goal, root_extra={"role": "other"})
    assert first.pending_run_passport
    first_exec = executor.run(first.graph, first_args, run_id="run-narrative", inputs=inputs)
    classify_node = next(n for n in first.graph.nodes if n.node_key.atom_name == "identify.classify")
    passport = first_exec.evidence_by_key[classify_node.node_key][0]

    second, second_args = planner.plan_ex(goal, run_passport=passport, root_extra={"role": "other"})
    assert second.feasibility.feasible, second.feasibility.reason
    node_names = {n.node_key.atom_name for n in second.graph.nodes}
    assert {"assess.judgment", "explain.internal", "explain.external", "out.narrative",
            "verify.grounding"} <= node_names

    second_exec = executor.run(
        second.graph, second_args, run_id="run-narrative", run_passport=passport, inputs=inputs,
        prior_node_runs=first_exec.node_runs, prior_evidence=first_exec.evidence_by_key,
    )
    assert second_exec.succeeded, second_exec.node_runs[second.graph.root].error

    result = second_exec.evidence_by_key[second.graph.root]
    narratives = [e for e in result if isinstance(e, NarrativeOutput)]
    assert len(narratives) == 1, "заземлённый нарратив обязан пройти verify.grounding и дойти до корня"
    assert narratives[0].claims[0].value in _FAKE_VALUES.values()


def test_P19_4_full_analysis_of_an_ifrs_report_gets_a_non_empty_external_context(
    severstal_text, make_fake_locate_and_read_value_executors, make_fake_structure_executor,
    make_fake_entities_executor, monkeypatch,
):
    """Часть 19 доводки: настоящий `explain.external` в детерминированном графе, подставной только поисковик.
    До исправления юрисдикции каждый факт МСФО-отчёта отбрасывался (`standard="ifrs"` → `None`)."""
    from finengine.contracts import ExternalFactRef
    from finengine.skills import web_search as ws

    asked: list[str] = []

    async def _search(query, max_results=5, *, topic="general", days=None):  # noqa: ARG001
        asked.append(query)
        # заголовок повторяет запрос: фильтр «другая компания с похожим названием» находку пропускает
        return [ws.WebSearchResult(title=query, url=f"https://www.interfax.ru/business/{len(asked)}",
                                   content="", published_date="2025-02-01")]

    monkeypatch.setattr(ws, "is_enabled", lambda: True)
    monkeypatch.setattr(ws, "web_search", _search)
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors(_FAKE_VALUES)
    registry = build_registry(
        profile_id="ifrs_nf_ru", locate_executor=locate_exec, read_value_executor=read_value_exec,
        structure_executor=make_fake_structure_executor({}), entities_executor=make_fake_entities_executor([]),
        judgment_executor=_fake_judgment_batch, explanation_executor=_fake_explanation_batch,
        narrative_executor=_fake_narrative_executor,
    )
    planner = GraphPlanner(registry=registry, policy=default_policy("ifrs_nf_ru"))
    goal = Target(output_type="NarrativeOutput", entity_ref="severstal", period_ref="p-2024-fy")
    inputs = RunInputs(document_text=severstal_text, role="other")
    executor = GraphExecutor(registry=registry)

    first, first_args = planner.plan_ex(goal, root_extra={"role": "other"})
    first_exec = executor.run(first.graph, first_args, run_id="run-external", inputs=inputs)
    classify_node = next(n for n in first.graph.nodes if n.node_key.atom_name == "identify.classify")
    passport = first_exec.evidence_by_key[classify_node.node_key][0]
    assert passport.classification.standard == "ifrs", "в паспорте — ось стандарта, не id ячейки"

    second, second_args = planner.plan_ex(goal, run_passport=passport, root_extra={"role": "other"})
    second_exec = executor.run(
        second.graph, second_args, run_id="run-external", run_passport=passport, inputs=inputs,
        prior_node_runs=first_exec.node_runs, prior_evidence=first_exec.evidence_by_key,
    )

    assert second_exec.succeeded, second_exec.node_runs[second.graph.root].error
    external = next(n for n in second.graph.nodes if n.node_key.atom_name == "explain.external")
    facts = second_exec.evidence_by_key[external.node_key]
    assert asked, "поиск не вызывался"
    assert facts and all(isinstance(fact, ExternalFactRef) and fact.jurisdiction == "RU" for fact in facts)


def test_fabricated_claim_blocks_the_narrative_from_reaching_the_root(
    severstal_text, make_fake_locate_and_read_value_executors, make_fake_structure_executor,
    make_fake_entities_executor,
):
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors(_FAKE_VALUES)

    def fabricating_narrative(args, ctx):  # noqa: ARG001
        deps = ctx.dependencies()
        judgments = [e for e in deps if isinstance(e, Judgment)]
        if not judgments:
            return []
        claim = NumericClaimRecord(text="выручка выросла на 40%", kind="financial", value=0.40, evidence_id=None)
        return [NarrativeOutput(run_id=ctx.run_id, text="Выручка выросла на 40%.", claims=[claim])]

    registry = build_registry(
        profile_id="ifrs_nf_ru", locate_executor=locate_exec, read_value_executor=read_value_exec,
        structure_executor=make_fake_structure_executor({}), entities_executor=make_fake_entities_executor([]),
        judgment_executor=_fake_judgment_batch, explanation_executor=_fake_explanation_batch,
        external_executor=_fake_external_executor, narrative_executor=fabricating_narrative,
    )
    policy = default_policy("ifrs_nf_ru")
    planner = GraphPlanner(registry=registry, policy=policy)
    goal = Target(output_type="NarrativeOutput", entity_ref="severstal", period_ref="p-2024-fy")
    inputs = RunInputs(document_text=severstal_text, role="other")
    executor = GraphExecutor(registry=registry)

    first, first_args = planner.plan_ex(goal, root_extra={"role": "other"})
    first_exec = executor.run(first.graph, first_args, run_id="run-fab", inputs=inputs)
    classify_node = next(n for n in first.graph.nodes if n.node_key.atom_name == "identify.classify")
    passport = first_exec.evidence_by_key[classify_node.node_key][0]

    second, second_args = planner.plan_ex(goal, run_passport=passport, root_extra={"role": "other"})
    second_exec = executor.run(
        second.graph, second_args, run_id="run-fab", run_passport=passport, inputs=inputs,
        prior_node_runs=first_exec.node_runs, prior_evidence=first_exec.evidence_by_key,
    )
    assert second_exec.succeeded, second_exec.node_runs[second.graph.root].error
    result = second_exec.evidence_by_key[second.graph.root]
    assert not any(isinstance(e, NarrativeOutput) for e in result), (
        "verify.grounding обязан блокировать текст с придуманным числом до корня графа"
    )
