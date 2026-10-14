"""A1.4 шаг 11 / A1.11.7: цикл в контрактах атомов обнаруживается ДО запуска
`GraphExecutor`, не разрешается бесконечным углублением — «100% синтетических
циклических графов» (краевой случай 9, 99-matrica.md)."""
from __future__ import annotations

import pytest

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import GraphPlanner


def test_direct_two_node_cycle_is_rejected(atom, target, policy):
    registry = AtomRegistry([
        atom("calc.a", ("A",), requires=("B",)),
        atom("calc.b", ("B",), requires=("A",)),
    ])
    result = GraphPlanner(registry=registry, policy=policy).plan(target("A"))

    assert not result.feasibility.feasible
    assert "цикл" in (result.feasibility.reason or "")


@pytest.mark.parametrize("length", [3, 5, 8])
def test_longer_cycles_of_various_length_are_all_caught(atom, target, policy, length):
    """golden-фикстур `planner-06-would-be-cyclic-negative` — двухузловой
    случай; здесь то же свойство на цепочках произвольной длины, не только
    на самом коротком цикле."""
    atoms = []
    for i in range(length):
        produces = (f"C{i}",)
        requires = (f"C{(i + 1) % length}",)
        atoms.append(atom(f"calc.{i}", produces, requires=requires))
    registry = AtomRegistry(atoms)

    result = GraphPlanner(registry=registry, policy=policy).plan(target("C0"))

    assert not result.feasibility.feasible
    assert "цикл" in (result.feasibility.reason or "")


def test_shared_dependency_without_a_cycle_is_not_mistaken_for_one(atom, target, policy):
    """Диамант-форма (A требует B и C, оба требуют D) — законный dedup/fan-in,
    не цикл. Детектор не должен путать «два пути к одному узлу» с «узел
    требует сам себя»."""
    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("B", "C"), requires=("DOC",)),
        atom("calc.a", ("A",), requires=("B", "C")),
    ])
    result = GraphPlanner(registry=registry, policy=policy).plan(target("A"))

    assert result.feasibility.feasible
