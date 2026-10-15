"""A1.11.11: «на синтетическом графе до 5000 логических узлов построение и
валидация плана на типовом CI-исполнителе укладываются в 500 мс без сетевых
вызовов».

Форма графа — сбалансированное дерево (каждый узел требует ДВУХ более ранних),
не линейная цепочка в 5000 звеньев: реальные графы этого продукта — fan-in от
множества фактов к одной цели (см. golden-фикстур `planner-02-full-analysis-
fan-in`), не километровые цепочки. Дерево к тому же безопасно для рекурсивного
`_visit`/`_expand_atom` (глубина ~log2(5000)≈13), тогда как цепочка той же
длины уперлась бы в предел рекурсии CPython."""
from __future__ import annotations

import time

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import GraphPlanner


def _binary_tree_registry(atom, *, num_nodes: int) -> AtomRegistry:
    """Узел `i` требует своих «детей» `2i+1`/`2i+2` (max-heap форма) — запрос
    КОРНЯ (`N0`) обязан утянуть за собой ВСЕ 5000 узлов вплоть до листьев."""
    atoms = []
    for i in range(num_nodes):
        children = tuple(f"N{c}" for c in (2 * i + 1, 2 * i + 2) if c < num_nodes)
        kind = "leaf.value" if not children else "calc.node"
        atoms.append(atom(f"{kind}_{i}", (f"N{i}",), requires=children))
    return AtomRegistry(atoms)


def test_five_thousand_node_tree_builds_within_budget(atom, target, policy):
    num_nodes = 5000
    registry = _binary_tree_registry(atom, num_nodes=num_nodes)
    planner = GraphPlanner(registry=registry, policy=policy)
    root_concept = "N0"

    started = time.perf_counter()
    result = planner.plan(target(root_concept))
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert result.feasibility.feasible
    assert len(result.graph.nodes) == num_nodes
    assert elapsed_ms < 500, f"построение плана заняло {elapsed_ms:.1f} мс — бюджет A1.11.11 превышен"
