"""Синтетический генератор `PlanGraph` для проверки свойств планировщика (C0.1).

`GraphPlanner` — это Срез 3 и сюда не входит. Этот модуль даёт независимый от
любой конкретной реализации способ породить МНОГО случайных, но по построению
корректных графов (и явно испорченный граф с циклом), чтобы будущий
планировщик можно было гонять на распределении входов, а не только на
шести вручную написанных сценариях в `manual/`.

Каждый узел в `generate_random_dag` разрешено требовать только УЖЕ созданные
более ранние узлы — это делает граф ацикличным по построению, а не проверкой
постфактум.
"""
from __future__ import annotations

import random

from finengine.contracts import NodeKey, PlanGraph, PlanNode


def _node_key(seed: int, i: int) -> NodeKey:
    return NodeKey(
        atom_name=f"synthetic.atom_{i}",
        atom_version="1.0.0",
        args_fingerprint=f"seed={seed};node={i}",
    )


def generate_random_dag(*, num_nodes: int, seed: int, max_requires_per_node: int = 3) -> PlanGraph:
    """Случайный, ацикличный по построению граф из `num_nodes` узлов."""
    if num_nodes < 1:
        raise ValueError("num_nodes must be >= 1")
    rng = random.Random(seed)
    keys = [_node_key(seed, i) for i in range(num_nodes)]
    nodes: list[PlanNode] = []
    for i, key in enumerate(keys):
        pool = keys[:i]  # только более ранние узлы — цикл невозможен
        k = rng.randint(0, min(max_requires_per_node, len(pool)))
        requires = rng.sample(pool, k) if pool else []
        nodes.append(
            PlanNode(
                node_key=key,
                requires=requires,
                produces=[f"concept_{seed}_{i}"],
                sandbox_profile="compute_no_network",
            )
        )
    return PlanGraph(
        plan_id=f"synthetic-{seed}-{num_nodes}",
        root=keys[-1],
        nodes=nodes,
        graph_hash=f"sha256:synthetic-{seed}-{num_nodes}",
    )


def generate_cyclic_graph(*, seed: int) -> PlanGraph:
    """Заведомо испорченный двухузловой граф — для проверки, что детектор цикла его ловит."""
    a = _node_key(seed, 0)
    b = _node_key(seed, 1)
    nodes = [
        PlanNode(node_key=a, requires=[b], produces=["A"], sandbox_profile="compute_no_network"),
        PlanNode(node_key=b, requires=[a], produces=["B"], sandbox_profile="compute_no_network"),
    ]
    return PlanGraph(plan_id=f"synthetic-cyclic-{seed}", root=a, nodes=nodes, graph_hash=f"sha256:synthetic-cyclic-{seed}")


def _key_tuple(key: NodeKey) -> tuple[str, str, str]:
    return (key.atom_name, key.atom_version, key.args_fingerprint)


def is_acyclic(graph: PlanGraph) -> bool:
    """DFS с различением 'в обработке' и 'обработан' — находит цикл любой длины."""
    by_key = {_key_tuple(n.node_key): n for n in graph.nodes}
    visiting: set[tuple[str, str, str]] = set()
    visited: set[tuple[str, str, str]] = set()

    def visit(key: NodeKey) -> bool:
        kt = _key_tuple(key)
        if kt in visited:
            return True
        if kt in visiting:
            return False
        visiting.add(kt)
        node = by_key.get(kt)
        if node is not None:
            for req in node.requires:
                if not visit(req):
                    return False
        visiting.discard(kt)
        visited.add(kt)
        return True

    return all(visit(n.node_key) for n in graph.nodes)


def all_requires_resolve_to_known_nodes(graph: PlanGraph) -> bool:
    known = {_key_tuple(n.node_key) for n in graph.nodes}
    return all(_key_tuple(req) in known for n in graph.nodes for req in n.requires)
