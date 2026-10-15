"""A1.11.1: «при одинаковых `TargetNode`, доступных листьях, `RunPassport`,
`AtomRegistry` и `PlanPolicy` строится канонически одинаковый `PlanGraph` и
одинаковый хэш плана в 100% из 1000 генеративных прогонов» — и краевой случай
42 (99-matrica.md): другой порядок обхода реестра не меняет граф."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import GraphPlanner


def _moderately_complex_registry(atom):
    return AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("ASSETS", "LIABILITIES", "EQUITY", "REVENUE", "EXPENSES"), requires=("DOC",)),
        atom("calc.current_ratio", ("CURRENT_RATIO",), requires=("ASSETS", "LIABILITIES"),
             mandatory_gates=("verify.arithmetic",)),
        atom("calc.margin", ("MARGIN",), requires=("REVENUE", "EXPENSES"), mandatory_gates=("verify.arithmetic",)),
        # Namespaced produces — не "CURRENT_RATIO"/"MARGIN" (см. test_mandatory_gates.py
        # про то, почему совпадение имени гейта с гейтуемым концептом ломает
        # producers_of()).
        atom("verify.arithmetic", ("Verified:CURRENT_RATIO", "Verified:MARGIN")),
        atom("analyze.full", ("FULL_ANALYSIS",), requires=("CURRENT_RATIO", "MARGIN", "EQUITY")),
    ])


def test_the_same_inputs_produce_the_same_graph_and_hash_a_thousand_times(atom, target, policy):
    registry = _moderately_complex_registry(atom)
    planner = GraphPlanner(registry=registry, policy=policy)
    goal = target("FULL_ANALYSIS")

    first = planner.plan(goal)
    assert first.feasibility.feasible

    for _ in range(1000):
        again = planner.plan(goal)
        assert again.graph == first.graph
        assert again.graph.graph_hash == first.graph.graph_hash


def test_reordering_the_registry_does_not_change_the_hash(atom, target, policy):
    forward = AtomRegistry(list(_moderately_complex_registry(atom).all()))
    backward = AtomRegistry(list(reversed(_moderately_complex_registry(atom).all())))
    goal = target("FULL_ANALYSIS")

    result_forward = GraphPlanner(registry=forward, policy=policy).plan(goal)
    result_backward = GraphPlanner(registry=backward, policy=policy).plan(goal)

    assert result_forward.graph.graph_hash == result_backward.graph.graph_hash
    assert {n.node_key for n in result_forward.graph.nodes} == {n.node_key for n in result_backward.graph.nodes}
