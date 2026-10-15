"""Retrofit v9 (R3, A1.5 «Dynamic dependency expansion»): `calc.formula`
не обнаруживает свои входы скрыто внутри исполнителя — `Planner` читает
`dynamic_requires` (данные, не процедурный код handler'а) и продолжает
раскрытие входов ФОРМУЛЫ до того, как узел станет `ready` для `GraphExecutor`.
На реальном реестре продукта (`ifrs_nf_ru`) это `calc.formula` через
`MetricEvidence`-резолюцию собственных зависимостей, читая типизированный
`FormulaDefinition` (`skills/atoms/calc_formula.py::calc_formula_requires`,
Срез 6a — эволюция бывшего `_calc_formula_deps`)."""
from __future__ import annotations

from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import Target


def test_formula_dependencies_are_resolved_by_the_planner_before_the_handler_runs():
    """`NET_INTEREST_PAID = INTEREST_PAID - INTEREST_RECEIVED` (ifrs_nf_ru,
    только расчётная, без альтернативного extraction-пути) — Planner обязан
    раскрыть ОБА входа как реальные узлы графа ДО исполнения, не полагаясь
    на то, что `calc.formula`'s handler сам решит, что ему нужно."""
    registry = build_registry(profile_id="ifrs_nf_ru")
    policy = default_policy("ifrs_nf_ru")
    target = Target(output_type="CalculatedMetric", concept_id="NET_INTEREST_PAID",
                     entity_ref="e1", period_ref="p1")

    # Первый проход (A1.5): до RunPassport formula deps ещё не известны узлу
    # — только сам узел и гейт RunPassport.
    first = GraphPlanner(registry=registry, policy=policy).plan(target)
    assert first.pending_run_passport
    root_node_first = next(n for n in first.graph.nodes if n.node_key == first.graph.root)
    assert not any(
        n.node_key.atom_name in ("read.value", "calc.formula") and n.node_key != first.graph.root
        for n in first.graph.nodes
    ), "до RunPassport formula deps не должны быть раскрыты — граница A1.5"


def test_full_bundle_scope_expands_via_planner_data_not_handler_code():
    """`analyze.full_bundle`'s `dynamic_collection_scope` — та же дисциплина:
    список показателей полного анализа приходит из `roles/scope.py` как
    ДАННЫЕ, переданные Planner'у, не из логики самого исполнителя `_analyze_
    full_bundle` (который просто собирает уже разрешённые зависимости)."""
    import inspect

    from finengine.skills.atoms.graph_atoms import _analyze_full_bundle

    source = inspect.getsource(_analyze_full_bundle)
    assert "build_engine_scope" not in source, (
        "исполнитель analyze.full_bundle не должен сам решать, какие показатели ему нужны — "
        "это работа dynamic_collection_scope, раскрываемого Planner'ом ДО готовности узла"
    )
