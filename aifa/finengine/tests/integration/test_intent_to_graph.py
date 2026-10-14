"""Срез 4 (B4): «`GoalIR` → `PlanGraph` → крупные адаптеры существующих
скриптов» — от СВОБОДНОГО ЗАПРОСА, не от вручную собранного `TargetNode`, как
делали тесты Среза 3 (`test_graph_point_metric.py` и соседи). Это ровно то, о
чём карточка среза говорит «как ПОЛНЫЙ рабочий маршрут, не тестовую
песочницу»: единственное отличие от боевого пути — модельная граница
`intent.parse` (`complete_fn`) и `locate`/`read.value`
(`make_fake_locate_and_read_value_executors`, тот же приём, что уже утвердил
Срез 3 для `extract.scalar` до его разбора в Срезе 5a), а не сам
граф/адаптеры/планировщик — они настоящие, как и в `test_graph_*` Среза 3.
"""
from __future__ import annotations

import json

from finengine.contracts import (
    CalculatedMetric, EvidenceBundleV2, ExtractedMetric, NarrativeOutput, ReportOutput,
)
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.atoms.intent import parse_intent, root_from_goal
from finengine.skills.spec.graph_executor import GraphExecutor, RunInputs
from finengine.skills.spec.graph_planner import GraphPlanner

from tests.integration.test_graph_analysis_narrative import (
    _fake_explanation_batch,
    _fake_external_executor,
    _fake_judgment_batch,
    _fake_narrative_executor,
)


def _fake_llm_response(payload: dict):
    async def _complete(system: str, prompt: str, *, model_version=None) -> str:
        return json.dumps(payload, ensure_ascii=False)
    return _complete


async def _run_to_completion(root_item, *, registry, policy, inputs, run_id):
    """Тот же двухпроходный протокол, что `test_graph_point_metric.py` —
    первый проход останавливается на классификации, второй достраивает граф
    уже со знанием `RunPassport` (A1.5)."""
    planner = GraphPlanner(registry=registry, policy=policy)
    executor = GraphExecutor(registry=registry)

    first, first_args = planner.plan_ex(root_item.root, root_extra=root_item.root_extra)
    first_exec = executor.run(first.graph, first_args, run_id=run_id, inputs=inputs)
    classify_node = next(n for n in first.graph.nodes if n.node_key.atom_name == "identify.classify")
    passport = first_exec.evidence_by_key[classify_node.node_key][0]

    second, second_args = planner.plan_ex(root_item.root, run_passport=passport, root_extra=root_item.root_extra)
    second_exec = executor.run(
        second.graph, second_args, run_id=run_id, run_passport=passport, inputs=inputs,
        prior_node_runs=first_exec.node_runs, prior_evidence=first_exec.evidence_by_key,
    )
    return second, second_exec


async def test_extract_registered_metric_query_reaches_the_real_graph_via_intent_parse(severstal_text, make_fake_locate_and_read_value_executors):
    fake_llm = _fake_llm_response({
        "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
        "entity_ref": "severstal", "period_ref": "p-2024-fy", "role": None,
        "unresolved": [], "ambiguities": [],
    })
    goal = await parse_intent("какая выручка у Северстали за 2024 год", complete_fn=fake_llm)
    assert goal.intent_kind == "extract_registered_metric"
    roots = root_from_goal(goal)
    assert len(roots) == 1

    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({"SALES_REVENUE": 987_654.0})
    registry = build_registry(
        profile_id="ifrs_nf_ru", locate_executor=locate_exec, read_value_executor=read_value_exec,
    )
    policy = default_policy("ifrs_nf_ru")
    inputs = RunInputs(document_text=severstal_text, role="other")

    result, execution = await _run_to_completion(
        roots[0], registry=registry, policy=policy, inputs=inputs, run_id="run-intent-point",
    )
    assert execution.succeeded
    # Срез 6a: корень — последний гейт цепочки read.value (`verify.bounds`),
    # не read.value сам по себе; гейт пропускает и находку, и wrapper (см.
    # `tests/integration/test_graph_point_metric.py` за тем же изменением).
    root_evidence = execution.evidence_by_key[result.graph.root]
    metric = next(e for e in root_evidence if isinstance(e, ExtractedMetric))
    assert metric.concept == "SALES_REVENUE"
    assert metric.scalar_fact.value == 987_654.0

    # Тот же акцепт-сценарий #3, что в Срезе 3 — точечный запрос, пришедший
    # через intent.parse, тоже не разворачивает полный реестр.
    scalar_nodes = [n for n in result.graph.nodes if n.node_key.atom_name == "read.value"]
    assert len(scalar_nodes) == 1


async def test_calculate_ad_hoc_metric_query_reaches_the_real_graph_via_intent_parse(
    severstal_text, make_fake_locate_and_read_value_executors,
):
    """Срез 27: CALCULATE_AD_HOC_METRIC реально исполняется через production
    runtime, не только через unit-вызов `calc_formula_executor` напрямую —
    ad-hoc формула с двумя зависимостями (обе реальные, зарегистрированные
    концепты) считается настоящим `calc.formula`, и evidence_id обеих
    зависимостей попадает в `DerivedFact.inputs`."""
    term = "выручка минус чистая прибыль"
    fake_llm = _fake_llm_response({
        "intent_kind": "calculate_ad_hoc_metric", "targets": [],
        "ad_hoc_formula": {"expression": "SALES_REVENUE - NET_INCOME", "dependencies": ["SALES_REVENUE", "NET_INCOME"]},
        "entity_ref": "severstal", "period_ref": "p-2024-fy", "role": None,
        "unresolved": [term], "ambiguities": [],
    })
    goal = await parse_intent("посчитай выручку минус чистая прибыль у Северстали за 2024 год", complete_fn=fake_llm)
    assert goal.intent_kind == "calculate_ad_hoc_metric"
    assert goal.parameters["ad_hoc_formula"]["expression"] == "SALES_REVENUE - NET_INCOME"
    roots = root_from_goal(goal)
    assert len(roots) == 1
    assert roots[0].root.output_type == "CalculatedMetric"
    assert roots[0].root_extra["ad_hoc_formula_expression"] == "SALES_REVENUE - NET_INCOME"

    values = {"SALES_REVENUE": 1_000_000.0, "NET_INCOME": 120_000.0}
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors(values)
    registry = build_registry(
        profile_id="ifrs_nf_ru", locate_executor=locate_exec, read_value_executor=read_value_exec,
    )
    policy = default_policy("ifrs_nf_ru")
    inputs = RunInputs(document_text=severstal_text, role="other")

    result, execution = await _run_to_completion(
        roots[0], registry=registry, policy=policy, inputs=inputs, run_id="run-intent-ad-hoc-calc",
    )
    assert execution.succeeded, execution.node_runs[result.graph.root].error
    root_evidence = execution.evidence_by_key[result.graph.root]
    calculated = next(e for e in root_evidence if isinstance(e, CalculatedMetric))
    assert calculated.derived_fact.value == 1_000_000.0 - 120_000.0
    assert calculated.definition_origin == "ad_hoc"
    # calc.formula's own requires are the two verify.bounds-gated dependency
    # nodes (their evidence_id is the LATEST version after norm.unit/norm.time
    # superseded the raw read.value one) — evidence_id of each real dependency
    # must be the trace calc.formula actually consumed, not invented.
    calc_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "calc.formula")
    dep_evidence_ids = {
        e.scalar_fact.evidence_id
        for dep_key in calc_node.requires
        for e in execution.evidence_by_key.get(dep_key, ())
        if isinstance(e, ExtractedMetric)
    }
    assert len(dep_evidence_ids) == 2
    assert dep_evidence_ids == set(calculated.derived_fact.inputs)


async def test_full_analysis_query_reaches_the_real_graph_via_intent_parse(
    severstal_text, make_fake_locate_and_read_value_executors, make_fake_structure_executor,
    make_fake_entities_executor,
):
    fake_llm = _fake_llm_response({
        "intent_kind": "full_analysis", "targets": [],  # модель дисциплинированно не перечисляет показатели
        "entity_ref": "severstal", "period_ref": "p-2024-fy", "role": "other",
        "unresolved": [], "ambiguities": [],
    })
    goal = await parse_intent("сделай полный финансовый анализ Северстали за 2024 год", complete_fn=fake_llm)
    assert goal.intent_kind == "full_analysis"
    assert goal.targets == []
    roots = root_from_goal(goal)
    assert len(roots) == 1
    assert roots[0].root.output_type == "ReportOutput"

    values = {"SALES_REVENUE": 1_000_000.0, "NET_INCOME": 120_000.0, "INTEREST_PAID": 9_000.0, "INTEREST_RECEIVED": 2_000.0}
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors(values)
    registry = build_registry(
        profile_id="ifrs_nf_ru", locate_executor=locate_exec, read_value_executor=read_value_exec,
        structure_executor=make_fake_structure_executor({}), entities_executor=make_fake_entities_executor([]),
        judgment_executor=_fake_judgment_batch, explanation_executor=_fake_explanation_batch,
        external_executor=_fake_external_executor, narrative_executor=_fake_narrative_executor,
    )
    policy = default_policy("ifrs_nf_ru")
    inputs = RunInputs(document_text=severstal_text, role="other")

    result, execution = await _run_to_completion(
        roots[0], registry=registry, policy=policy, inputs=inputs, run_id="run-intent-full",
    )
    assert execution.succeeded, execution.node_runs[result.graph.root].error
    root_values = execution.evidence_by_key[result.graph.root]
    assert any(isinstance(value, ReportOutput) for value in root_values)
    bundle = next(
        value
        for values in execution.evidence_by_key.values()
        for value in values
        if isinstance(value, EvidenceBundleV2)
    )
    assert {f.concept for f in bundle.scalar_facts} >= {"SALES_REVENUE", "NET_INCOME"}
    node_names = {node.node_key.atom_name for node in result.graph.nodes}
    assert {"out.narrative", "verify.grounding", "verify.citation", "out.report"} <= node_names


async def test_explain_change_for_financial_investments_reaches_scoped_narrative_graph(
    severstal_text, make_fake_locate_and_read_value_executors,
    make_fake_structure_executor, make_fake_entities_executor,
):
    concepts = ["LONG_TERM_FINANCIAL_INVESTMENTS", "SHORT_TERM_FINANCIAL_INVESTMENTS"]
    fake_llm = _fake_llm_response({
        "intent_kind": "explain_change", "targets": [],
        "analysis_concepts": concepts, "period_scope": "all_available",
        # This test enters below ProductionContext; the production ingress
        # fills an omitted entity from the active document's frozen passport.
        "entity_ref": "severstal", "period_ref": None, "role": None,
        "unresolved": [], "ambiguities": [],
    })
    goal = await parse_intent(
        "Объясни разницу финансовых вложений между всеми периодами",
        complete_fn=fake_llm,
    )
    roots = root_from_goal(goal)
    assert len(roots) == 1

    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({
        "LONG_TERM_FINANCIAL_INVESTMENTS": 120.0,
        "SHORT_TERM_FINANCIAL_INVESTMENTS": 30.0,
    })
    registry = build_registry(
        profile_id="rsbu_nf_ru", locate_executor=locate_exec,
        read_value_executor=read_value_exec,
        structure_executor=make_fake_structure_executor({}),
        entities_executor=make_fake_entities_executor([]),
        judgment_executor=_fake_judgment_batch,
        explanation_executor=_fake_explanation_batch,
        external_executor=_fake_external_executor,
        narrative_executor=_fake_narrative_executor,
    )
    result, execution = await _run_to_completion(
        roots[0], registry=registry, policy=default_policy("rsbu_nf_ru"),
        inputs=RunInputs(document_text=severstal_text, role="other"),
        run_id="run-intent-explain-financial-investments",
    )

    assert execution.succeeded, execution.node_runs[result.graph.root].error
    bundle = next(
        value
        for values in execution.evidence_by_key.values()
        for value in values
        if isinstance(value, EvidenceBundleV2)
    )
    assert {fact.concept for fact in bundle.scalar_facts} == set(concepts), {
        f"{key.atom_name}:{key.args_fingerprint}": [type(value).__name__ for value in values]
        for key, values in execution.evidence_by_key.items()
        if key.atom_name in {"locate", "read.value", "analyze.full_bundle"}
    }
    assert any(
        isinstance(value, NarrativeOutput)
        for value in execution.evidence_by_key[result.graph.root]
    ), {
        f"{key.atom_name}:{key.args_fingerprint}": [type(value).__name__ for value in values]
        for key, values in execution.evidence_by_key.items()
    }
    node_names = {node.node_key.atom_name for node in result.graph.nodes}
    assert {"ref.prior", "assess.judgment", "explain.internal", "out.narrative"} <= node_names


async def test_role_form_query_reaches_the_real_graph_via_intent_parse(sberbank_text, make_fake_locate_and_read_value_executors):
    from finengine.roles.scope import layouts

    row_specs = layouts()["roles"]["dgr"]["formats"]["bank"]["rows"]
    available = {row["enum"]: float(100 + i) for i, row in enumerate(row_specs[:5])}

    fake_llm = _fake_llm_response({
        "intent_kind": "form", "targets": [],
        "entity_ref": "sberbank", "period_ref": "p-2024-fy", "role": "dgr",
        "unresolved": [], "ambiguities": [],
    })
    goal = await parse_intent("заполни форму ДГР по Сбербанку за 2024 год", complete_fn=fake_llm)
    assert goal.intent_kind == "form"
    assert goal.role == "dgr"
    roots = root_from_goal(goal)
    assert roots[0].root_extra == {"role": "dgr"}

    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors(available)
    registry = build_registry(profile_id="ifrs_bank_ru", locate_executor=locate_exec, read_value_executor=read_value_exec)
    policy = default_policy("ifrs_bank_ru")
    inputs = RunInputs(document_text=sberbank_text, role="other")

    result, execution = await _run_to_completion(
        roots[0], registry=registry, policy=policy, inputs=inputs, run_id="run-intent-form",
    )
    assert execution.succeeded, execution.node_runs[result.graph.root].error
    form = execution.evidence_by_key[result.graph.root][0]
    assert form.role == "dgr"
    assert len(row_specs) == 309
    assert len(form.rows) == len(row_specs)
    assert any(r.available for r in form.rows)
    assert any(not r.available for r in form.rows)


async def test_unresolved_concept_never_reaches_the_graph_as_a_root():
    """Краевой случай 8 из 99-matrica.md: неизвестный концепт не должен
    доехать до `GraphPlanner` вовсе — `root_from_goal` на пустых `targets`
    просто не создаёт для него корень (в отличие от `full_analysis`/`form`,
    которые синтезируют корень из одного `intent_kind`,
    `extract_registered_metric` с пустыми `targets` — нулевой список корней,
    а не выдуманный)."""
    fake_llm = _fake_llm_response({
        "intent_kind": "extract_registered_metric", "targets": [],
        "entity_ref": "Яндекс", "period_ref": None, "role": None,
        "unresolved": ["мультипликатор счастья инвестора"], "ambiguities": [],
    })
    goal = await parse_intent("дай мультипликатор счастья инвестора для Яндекса", complete_fn=fake_llm)
    assert goal.unresolved == ["мультипликатор счастья инвестора"]
    assert root_from_goal(goal) == []
