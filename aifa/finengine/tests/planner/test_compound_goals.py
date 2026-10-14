from __future__ import annotations

import pytest
import threading

from finengine.contracts import NodeKey
from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import AvailableIndex, AvailableLeaf, GraphPlanner
from finengine.skills.spec.graph_executor import GraphExecutor


def _nodes(result):
    return {node.node_key.atom_name: node for node in result.graph.nodes}


def test_independent_goal_types_become_sibling_subgraphs(atom, target, policy):
    registry = AtomRegistry([
        atom("source.a", ("A",)), atom("source.b", ("B",)),
        atom("goal.join", ("CompoundResult",), key_fields=("scenario_refs",)),
    ])
    result, _ = GraphPlanner(registry=registry, policy=policy).plan_many([
        (target("A"), {}, "extract", "extract_registered_metric", ()),
        (target("B"), {}, "calculate", "calculate_ad_hoc_metric", ()),
    ])

    nodes = _nodes(result)
    assert result.feasibility.feasible
    assert result.graph.root.atom_name == "goal.join"
    assert {scenario.goal_ref for scenario in result.graph.scenarios} == {"extract", "calculate"}
    assert nodes["source.a"].requires == []
    assert nodes["source.b"].requires == []
    assert set(nodes["goal.join"].requires) == {
        nodes["source.a"].node_key, nodes["source.b"].node_key,
    }


def test_dependent_goal_root_waits_for_upstream_scenario(atom, target, policy):
    registry = AtomRegistry([
        atom("extract.profit", ("PROFIT",)), atom("calc.margin", ("MARGIN",)),
        atom("goal.join", ("CompoundResult",), key_fields=("scenario_refs",)),
    ])
    result, _ = GraphPlanner(registry=registry, policy=policy).plan_many([
        (target("PROFIT"), {}, "profit", "extract_registered_metric", ()),
        (target("MARGIN"), {}, "margin", "calculate_registered_metric", ("profit",)),
    ])
    nodes = _nodes(result)
    assert nodes["extract.profit"].node_key in nodes["calc.margin"].requires


def test_compound_goal_dependency_cycle_is_rejected_before_execution(atom, target, policy):
    registry = AtomRegistry([
        atom("source.a", ("A",)), atom("source.b", ("B",)),
        atom("goal.join", ("CompoundResult",), key_fields=("scenario_refs",)),
    ])
    with pytest.raises(ValueError, match="cycle"):
        GraphPlanner(registry=registry, policy=policy).plan_many([
            (target("A"), {}, "a", "extract_registered_metric", ("b",)),
            (target("B"), {}, "b", "calculate_registered_metric", ("a",)),
        ])


def test_independent_scenarios_are_physically_dispatched_in_parallel(atom, target, policy):
    a_started = threading.Event()
    b_started = threading.Event()

    def run_a(*_args):
        a_started.set()
        assert b_started.wait(1), "scenario B was not dispatched concurrently"
        return []

    def run_b(*_args):
        b_started.set()
        assert a_started.wait(1), "scenario A was not dispatched concurrently"
        return []

    registry = AtomRegistry([
        atom("source.a", ("A",), executor=run_a),
        atom("source.b", ("B",), executor=run_b),
        atom("goal.join", ("CompoundResult",), key_fields=("scenario_refs",), executor=lambda *_: []),
    ])
    build, args = GraphPlanner(registry=registry, policy=policy).plan_many([
        (target("A"), {}, "a", "extract_registered_metric", ()),
        (target("B"), {}, "b", "calculate_ad_hoc_metric", ()),
    ])
    result = GraphExecutor(registry=registry).run(build.graph, args, run_id="compound")
    assert result.succeeded


def test_a_ready_node_shared_by_two_goals_does_not_conflict_on_their_root_extra(atom, target, policy):
    """Разбор прогона на dev 09.10.2026: `ingest.document` уже лежал в Case, а у частей разный `root_extra`
    (форма — `requested_concepts`, ad-hoc — `ad_hoc_*`). Готовый узел один, и его значимые поля тоже, поэтому
    чужой `extra` запрашивающей цепочки не должен делать его «конфликтующим»."""
    registry = AtomRegistry([
        atom("root.a", ("A",), requires=("DOC",)), atom("root.b", ("B",), requires=("DOC",)),
        atom("goal.join", ("CompoundResult",), key_fields=("scenario_refs",)),
    ])
    available = AvailableIndex()
    available.put(
        "DOC", AvailableLeaf(node_key=NodeKey(atom_name="ingest.document", atom_version="1.0.0", args_fingerprint="e1")),
        entity_ref="e1", period_ref="p1",
    )

    result, args = GraphPlanner(registry=registry, policy=policy).plan_many([
        (target("A"), {"requested_concepts": "SALES_REVENUE"}, "a", "extract_registered_metric", ()),
        (target("B"), {"ad_hoc_spec_id": "spec-1"}, "b", "calculate_ad_hoc_metric", ()),
    ], available=available)

    assert result.feasibility.feasible
    assert {node.node_key.atom_name for node in result.graph.nodes} >= {"ingest.document", "root.a", "root.b"}


def test_a_node_args_conflict_names_the_fields_that_differ(atom, target, policy):
    """На dev конфликт повторяется, а из сообщения не видно, какое поле разошлось: диагноз по логу невозможен."""
    registry = AtomRegistry([
        atom("root.a", ("A",), requires=("DOC",)), atom("root.b", ("B",), requires=("DOC",)),
        atom("doc.read", ("DOC",), key_fields=("entity_ref",)),
        atom("goal.join", ("CompoundResult",), key_fields=("scenario_refs",)),
    ])
    with pytest.raises(ValueError, match=r"conflicting NodeArgs.*extra.*requested_concepts"):
        GraphPlanner(registry=registry, policy=policy).plan_many([
            (target("A"), {"requested_concepts": "SALES_REVENUE"}, "a", "extract_registered_metric", ()),
            (target("B"), {}, "b", "calculate_ad_hoc_metric", ()),
        ])
