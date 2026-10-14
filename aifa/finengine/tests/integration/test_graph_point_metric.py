"""Сценарий «точечный показатель» (B3, Definition of Done): один показатель
через настоящий `PlanGraph`+`GraphExecutor`, не изолированный код. Заодно
сквозной приёмочный сценарий #3 (99-matrica.md): точечный запрос не
разворачивает полный реестр."""
from __future__ import annotations

from finengine.contracts import ExtractedMetric
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.spec.graph_executor import GraphExecutor, RunInputs
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import Target


def test_single_metric_flows_through_the_real_planner_and_executor(severstal_text, make_fake_locate_and_read_value_executors):
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({"SALES_REVENUE": 987_654.0})
    registry = build_registry(
        profile_id="ifrs_nf_ru", locate_executor=locate_exec, read_value_executor=read_value_exec,
    )
    policy = default_policy("ifrs_nf_ru")
    planner = GraphPlanner(registry=registry, policy=policy)
    # Retrofit v9 (R3.3/R4.4): «извлечь SALES_REVENUE» — Target(output_type=
    # ExtractedMetric), не голый Target(concept=SALES_REVENUE).
    goal = Target(output_type="ExtractedMetric", concept_id="SALES_REVENUE",
                  entity_ref="severstal", period_ref="p-2024-fy")
    inputs = RunInputs(document_text=severstal_text, role="other")
    executor = GraphExecutor(registry=registry)

    # Первый проход (A1.5): раскрытие останавливается на границе классификации.
    first, first_args = planner.plan_ex(goal)
    assert first.pending_run_passport
    first_exec = executor.run(first.graph, first_args, run_id="run-1", inputs=inputs)
    # Корень сам needs_run_passport (read.value) — он ОСТАЁТСЯ planned до
    # второго прохода, не выполняется по усечённому первому (регрессия A2:
    # tests/executor/test_run_passport_gating.py). succeeded в первом проходе
    # — только у identify.classify/ingest.document.
    assert first_exec.node_runs[first.graph.root].status == "planned"

    classify_node = next(n for n in first.graph.nodes if n.node_key.atom_name == "identify.classify")
    passport = first_exec.evidence_by_key[classify_node.node_key][0]
    assert passport.classification.profile_id == "ifrs_nf_ru"

    # Второй проход — с RunPassport (A1.11.9); ЗАЩИТА ИДЕМПОТЕНТНОСТИ (A2.1.6):
    # ingest.document/identify.classify не выполняются повторно, их результат
    # из первого прохода просто переиспользуется.
    #
    # Срез 6a: КОРЕНЬ графа больше НЕ совпадает между проходами байт-в-байт —
    # `read.value` объявляет `mandatory_gates=("norm.unit","norm.time",
    # "verify.bounds")`, а гейты раскрываются только вместе с остальными
    # requires атома, то есть только на этом, втором проходе (`_expand_atom`
    # возвращает `node_key` РАНЬШЕ гейтов на первом проходе, пока `run_passport
    # is None` — см. `graph_planner.py`). Второй проход честно возвращает
    # ПОСЛЕДНИЙ узел цепочки гейтов как корень — то же самое, зафиксированное
    # уже для синтетического случая в `tests/planner/test_mandatory_gates.py::
    # test_requesting_the_gated_concept_directly_still_returns_the_gate`:
    # «нельзя запросить обход обязательной проверки, назвав концепт корнем».
    second, second_args = planner.plan_ex(goal, run_passport=passport)
    assert not second.pending_run_passport
    assert second.graph.root.atom_name == "verify.bounds"

    second_exec = executor.run(
        second.graph, second_args, run_id="run-1", run_passport=passport, inputs=inputs,
        prior_node_runs=first_exec.node_runs, prior_evidence=first_exec.evidence_by_key,
    )

    assert second_exec.succeeded
    root_evidence = second_exec.evidence_by_key[second.graph.root]
    # Гейт пропускает дальше и находку, и сам wrapper (A1.7 — потребитель не
    # должен потерять метрику за проверкой): `finding` + `ExtractedMetric`.
    metric = next(e for e in root_evidence if isinstance(e, ExtractedMetric))
    assert metric.concept == "SALES_REVENUE"
    # R2/R5a: узел produces `ExtractedMetric`, ссылающийся на `ScalarFact` —
    # число живёт в `.scalar_fact.value`, не в самом wrapper'е.
    assert metric.scalar_fact.value == 987_654.0

    assert second_exec.node_runs[classify_node.node_key] is first_exec.node_runs[classify_node.node_key], (
        "identify.classify обязан переиспользоваться, не выполняться заново"
    )

    # Точечный запрос не разворачивает полный реестр (акцепт-сценарий #3):
    # среди узлов графа нет НИ ОДНОГО другого read.value-узла — все ~70
    # показателей ifrs_nf_ru зарегистрированы как СПОСОБНОСТИ реестра, но ни
    # один, кроме запрошенного, не попал в РАСКРЫТЫЙ граф. Тот же принцип для
    # его единственной зависимости `locate` (Срез 5a) и для гейтов, вставленных
    # Срезом 6a: ровно по одному узлу каждого гейта, не по одному на концепт
    # реестра.
    scalar_nodes = [n for n in second.graph.nodes if n.node_key.atom_name == "read.value"]
    assert len(scalar_nodes) == 1
    locate_nodes = [n for n in second.graph.nodes if n.node_key.atom_name == "locate"]
    assert len(locate_nodes) == 1
    for gate_name in ("norm.unit", "norm.time", "verify.bounds"):
        gate_nodes = [n for n in second.graph.nodes if n.node_key.atom_name == gate_name]
        assert len(gate_nodes) == 1, f"точечный запрос не должен размножать узлы {gate_name!r}"
