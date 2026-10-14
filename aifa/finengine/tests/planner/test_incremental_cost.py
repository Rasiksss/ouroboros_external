"""Retrofit v9 (R3, A1.4 «cost model v1»): среди нескольких достижимых
производителей выбирается minimum incremental plan cost — сумма весов
(`PlanPolicy.cost_weights`, по `AtomDef.cost_class`) ТОЛЬКО новых узлов после
dedup/reuse; уже существующий в графе/`AvailableIndex` узел стоит 0."""
from __future__ import annotations

from finengine.contracts import PlanPolicy
from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import AvailableIndex, AvailableLeaf, GraphPlanner
from finengine.skills.spec.target import InputSpec, OutputSpec, Target


def test_cheaper_reachable_producer_wins_without_any_policy_rule(atom, policy):
    """Кандидат A требует уже готовый лист (`DOC`, доступен в `AvailableIndex`
    — incremental cost 0 на эту ветвь); кандидат B требует дорогой модельный
    вызов. Без единой записи в `PlanPolicy.producer_priority` резолвер
    обязан выбрать A — не `ChoicePoint`."""
    from finengine.contracts import NodeKey

    registry = AtomRegistry([
        atom(
            "extract.cheap", (OutputSpec(output_type="CET1", concept="CET1"),),
            requires=(InputSpec(input_type="DOC"),), cost_class="free",
        ),
        atom(
            "extract.expensive", (OutputSpec(output_type="CET1", concept="CET1"),),
            requires=(InputSpec(input_type="FRESH_MODEL_CALL"),), cost_class="free",
        ),
        atom(
            "model.fresh_call", (OutputSpec(output_type="FRESH_MODEL_CALL"),),
            execution_kind="model", cost_class="model_call",
        ),
    ])
    available = AvailableIndex()
    doc_leaf = NodeKey(atom_name="case.evidence", atom_version="1.0.0", args_fingerprint="doc=already-here")
    available.put("DOC", AvailableLeaf(node_key=doc_leaf), entity_ref="e1", period_ref="p1")

    target = Target(output_type="CET1", concept_id="CET1", entity_ref="e1", period_ref="p1")
    result = GraphPlanner(registry=registry, policy=policy).plan(target, available=available)

    assert result.feasibility.feasible
    assert not result.choice_points
    assert result.graph.root.atom_name == "extract.cheap"
    assert not any(n.node_key.atom_name == "extract.expensive" for n in result.graph.nodes)
    assert not any(n.node_key.atom_name == "model.fresh_call" for n in result.graph.nodes), (
        "дорогая ветвь не должна быть материализована в графе — она даже не выбрана"
    )


def test_already_planned_node_costs_zero_and_wins_over_a_fresh_alternative(atom, policy):
    """Тот же принцип, но «уже существующий узел» — это узел, УЖЕ раскрытый
    ВЫШЕ по этому же графу (не внешний `AvailableIndex`), а не с нуля."""
    from finengine.skills.spec.target import Target

    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        # Оба производителя REVENUE_LIKE достижимы; A переиспользует DOC
        # (уже раскрыт другой веткой корня), B тянет отдельный дорогой вход.
        atom(
            "extract.reuse_doc", (OutputSpec(output_type="REVENUE_LIKE", concept="REVENUE_LIKE"),),
            requires=(InputSpec(input_type="DOC"),), cost_class="free",
        ),
        atom(
            "extract.own_feed", (OutputSpec(output_type="REVENUE_LIKE", concept="REVENUE_LIKE"),),
            requires=(InputSpec(input_type="OWN_FEED"),), cost_class="free",
        ),
        atom("io.own_feed", (OutputSpec(output_type="OWN_FEED"),), execution_kind="io", cost_class="cheap"),
        # Корень требует ОБА: DOC напрямую (форсируя его раскрытие первым) и
        # REVENUE_LIKE — так DOC уже есть в state, когда доходит очередь до
        # выбора между reuse_doc/own_feed.
        atom(
            "root.pick", ("COMBINED",),
            requires=(InputSpec(input_type="DOC"), InputSpec(input_type="REVENUE_LIKE", concept="REVENUE_LIKE")),
        ),
    ])
    target = Target(output_type="COMBINED", entity_ref="e1", period_ref="p1")
    result = GraphPlanner(registry=registry, policy=policy).plan(target)

    assert result.feasibility.feasible
    assert not result.choice_points
    assert any(n.node_key.atom_name == "extract.reuse_doc" for n in result.graph.nodes)
    assert not any(n.node_key.atom_name == "extract.own_feed" for n in result.graph.nodes)
    assert not any(n.node_key.atom_name == "io.own_feed" for n in result.graph.nodes)
