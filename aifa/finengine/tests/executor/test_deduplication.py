"""A2.1.2: один `NodeKey` исполняется не более одного раза в рамках `PlanRun`,
кроме явно зарегистрированной повторной попытки после технической ошибки."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_executor import GraphExecutor


def test_a_shared_dependency_executes_exactly_once(atom, target, build_plan):
    """Диамант: два потребителя одного и того же извлечённого факта — факт
    исполняется ОДИН раз, оба потребителя видят один и тот же результат."""
    calls = {"leaf": 0}

    def leaf_executor(args, ctx):  # noqa: ARG001
        calls["leaf"] += 1
        return ()

    registry = AtomRegistry([
        atom("leaf.value", ("SHARED",), executor=leaf_executor),
        atom("calc.left", ("LEFT",), requires=("SHARED",)),
        atom("calc.right", ("RIGHT",), requires=("SHARED",)),
        atom("calc.combine", ("COMBINED",), requires=("LEFT", "RIGHT")),
    ])
    result, node_args = build_plan(registry, target("COMBINED"))
    assert result.feasibility.feasible

    exec_result = GraphExecutor(registry=registry).run(result.graph, node_args, run_id="run-1")

    assert exec_result.succeeded
    assert calls["leaf"] == 1, "общая зависимость выполнилась больше одного раза"
