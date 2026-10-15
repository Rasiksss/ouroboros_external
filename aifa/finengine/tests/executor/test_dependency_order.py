"""A2.1.1: узел не запускается, пока все обязательные зависимости не имеют
допустимого конечного состояния — «нарушение порядка невозможно в 100%
генеративных тестов». Заодно A2.1.7: каждый `NodeRun` несёт время начала/
окончания, статус, число попыток, произведённые идентификаторы, профиль
песочницы."""
from __future__ import annotations

import pytest

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_executor import GraphExecutor, RunInputs


def _tree_registry(atom, *, num_nodes: int, order_log: list[str]):
    """Та же max-heap форма, что `tests/planner/test_plan_scale.py` — узел `i`
    требует `2i+1`/`2i+2`; запрос `N0` тянет за собой все узлы до листьев."""
    def make_executor(i: int):
        def _exec(args, ctx):  # noqa: ARG001
            order_log.append(f"N{i}")
            return ()
        return _exec

    atoms = []
    for i in range(num_nodes):
        children = tuple(f"N{c}" for c in (2 * i + 1, 2 * i + 2) if c < num_nodes)
        atoms.append(atom(f"calc.node_{i}", (f"N{i}",), requires=children, executor=make_executor(i)))
    return AtomRegistry(atoms)


@pytest.mark.parametrize("seed_nodes", [7, 15, 31, 63])
def test_every_node_runs_only_after_all_its_dependencies_finished(atom, target, build_plan, seed_nodes):
    order_log: list[str] = []
    registry = _tree_registry(atom, num_nodes=seed_nodes, order_log=order_log)
    result, node_args = build_plan(registry, target("N0"))
    assert result.feasibility.feasible

    exec_result = GraphExecutor(registry=registry).run(
        result.graph, node_args, run_id="run-1", inputs=RunInputs(),
    )

    assert exec_result.succeeded
    position = {name: i for i, name in enumerate(order_log)}
    for i in range(seed_nodes):
        for c in (2 * i + 1, 2 * i + 2):
            if c < seed_nodes:
                assert position[f"N{c}"] < position[f"N{i}"], (
                    f"N{i} выполнился раньше своей зависимости N{c} — нарушение порядка"
                )


def test_node_run_carries_the_fields_a2_1_7_requires(atom, target, build_plan):
    registry = AtomRegistry([atom("leaf.value", ("X",))])
    result, node_args = build_plan(registry, target("X"))

    exec_result = GraphExecutor(registry=registry).run(result.graph, node_args, run_id="run-1")

    run = exec_result.node_runs[result.graph.root]
    assert run.status == "succeeded"
    assert run.attempt == 1
    assert run.started_at is not None
    assert run.finished_at is not None
    assert run.sandbox_profile == "compute_no_network"
    assert run.node_key.atom_version == "1.0.0"
