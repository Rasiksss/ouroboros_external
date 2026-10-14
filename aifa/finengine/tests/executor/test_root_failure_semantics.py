"""A2.1.5: недостижимая обязательная ветвь не позволяет корневой вершине
получить статус успеха — ошибка узла блокирует только зависимые от него
ветки, независимые продолжаются (A2)."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_executor import GraphExecutor


def _boom(args, ctx):  # noqa: ARG001
    raise RuntimeError("извлечение упало по-настоящему")


def test_a_failing_dependency_blocks_the_root_but_not_an_unrelated_branch(atom, target, build_plan):
    registry = AtomRegistry([
        atom("extract.broken", ("BROKEN",), executor=_boom),
        atom("extract.fine", ("FINE",), executor=lambda *_a, **_k: ("ok",)),
        atom("calc.root", ("ROOT",), requires=("BROKEN", "FINE")),
    ])
    result, node_args = build_plan(registry, target("ROOT"))
    assert result.feasibility.feasible

    exec_result = GraphExecutor(registry=registry).run(result.graph, node_args, run_id="run-1")

    assert not exec_result.succeeded
    assert exec_result.node_runs[result.graph.root].status == "blocked"
    broken_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "extract.broken")
    fine_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "extract.fine")
    assert exec_result.node_runs[broken_node.node_key].status == "failed"
    assert exec_result.node_runs[fine_node.node_key].status == "succeeded", (
        "независимая ветка не должна пострадать от ошибки соседней"
    )


def test_two_independent_root_children_where_only_one_fails(atom, target, build_plan):
    """Более прямая иллюстрация «частичные ошибки не валят весь граф» (A2):
    результат независимой ветки остаётся доступным (в `evidence_by_key`),
    даже когда корень в целом заблокирован."""
    registry = AtomRegistry([
        atom("extract.broken", ("BROKEN",), executor=_boom),
        atom("extract.fine", ("FINE",), executor=lambda *_a, **_k: ("ok",)),
        atom("calc.root", ("ROOT",), requires=("BROKEN", "FINE")),
    ])
    result, node_args = build_plan(registry, target("ROOT"))
    exec_result = GraphExecutor(registry=registry).run(result.graph, node_args, run_id="run-1")

    fine_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "extract.fine")
    assert exec_result.evidence_by_key[fine_node.node_key] == ("ok",)
