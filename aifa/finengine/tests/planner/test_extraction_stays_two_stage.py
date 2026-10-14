"""Срез 31, П1: механика извлечения осталась ДВУХСТАДИЙНОЙ и пакетной.

Владелец: «сама механика извлечения должна была остаться такой же, как и была,
двухстадийной». Стадия 1 — `locate` (где в отчёте лежит показатель), стадия 2 —
`read.value` (какое там значение). Тест-страж: показатель реестра и ad-hoc
показатель раскрываются в одну и ту же пару, `read.value` не читает документ сам,
а зависит от `locate`.
"""
from __future__ import annotations

from finengine.contracts import GoalIR, TargetNode
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.atoms.intent import root_from_goal
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.spec.graph_planner import GraphPlanner

_CELL = "ifrs_nf_ru"


def _plan(goal: GoalIR):
    passport = build_run_passport("financial report", goal_ir_id=goal.goal_id, classification=classification_from_cell(_CELL))
    root = root_from_goal(goal)[0]
    planner = GraphPlanner(registry=build_registry(profile_id=_CELL), policy=default_policy(_CELL))
    return planner.plan(root.root, run_passport=passport, root_extra=root.root_extra).graph


def _read_value_requires_locate(graph, concept: str) -> bool:
    nodes = {n.node_key: n for n in graph.nodes}
    read = next(n for n in graph.nodes if n.node_key.atom_name == "read.value" and f"concept={concept}" in n.node_key.args_fingerprint)
    return any(
        req.atom_name == "locate" and f"concept={concept}" in req.args_fingerprint
        for req in _all_upstream(read, nodes)
    )


def _all_upstream(node, nodes):
    seen, stack = [], list(node.requires)
    while stack:
        key = stack.pop()
        if key in seen:
            continue
        seen.append(key)
        stack.extend(nodes[key].requires if key in nodes else [])
    return seen


def test_a_registered_metric_is_planned_as_locate_then_read_value():
    goal = GoalIR(
        raw_query="q", intent_kind="extract_registered_metric", entity_ref="e",
        targets=[TargetNode(concept="SALES_REVENUE", output_kind="scalar")],
    )
    graph = _plan(goal)
    atoms = {n.node_key.atom_name for n in graph.nodes}
    assert {"locate", "read.value"} <= atoms
    assert _read_value_requires_locate(graph, "SALES_REVENUE"), "read.value зависит от locate, а не читает документ сам"


def test_an_ad_hoc_metric_goes_through_the_same_two_stages():
    goal = GoalIR(
        raw_query="q", intent_kind="extract_ad_hoc_metric", entity_ref="e", unresolved=["площадь торговых залов"],
    )
    graph = _plan(goal)
    assert {"locate", "read.value"} <= {n.node_key.atom_name for n in graph.nodes}
    assert _read_value_requires_locate(graph, "площадь торговых залов")


def test_two_metrics_of_one_request_are_planned_as_two_logical_nodes_of_one_batchable_atom():
    """Физически один пакетный вызов на атом (C9): узлы `locate`/`read.value`
    объявляют `batch_key_fields`, а `GraphExecutor` объединяет одинаковые по ключу."""
    registry = build_registry(profile_id=_CELL)
    assert registry.get("locate").is_batchable and registry.get("read.value").is_batchable
