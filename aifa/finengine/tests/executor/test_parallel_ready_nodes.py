"""Готовые независимые узлы обычного графа идут одновременно, когда вызывающий это просит (ADR-0092).

Граф шагов хода собирается обычным (не составным): прежде исполнитель запускал его готовые узлы по очереди, хотя
у них нет связей. Режим `parallel_ready` включает пул потоков для любого графа; без него порядок прежний.
"""
from __future__ import annotations

import threading

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_executor import GraphExecutor

WAIT_SEC = 2


def _registry(atom) -> AtomRegistry:
    barrier = threading.Barrier(2, timeout=WAIT_SEC)

    def meet(*_args, **_kwargs):
        barrier.wait()        # BrokenBarrierError, если второй узел не начался одновременно
        return ("ok",)

    return AtomRegistry([
        atom("left", ("LEFT",), executor=meet),
        atom("right", ("RIGHT",), executor=meet),
        atom("join", ("DONE",), requires=("LEFT", "RIGHT")),
    ])


def test_independent_ready_nodes_run_at_the_same_time_when_asked(atom, target, build_plan) -> None:
    registry = _registry(atom)
    result, node_args = build_plan(registry, target("DONE"))

    run = GraphExecutor(registry=registry).run(result.graph, node_args, run_id="r1", parallel_ready=True)

    assert run.succeeded, "оба независимых узла должны были начаться одновременно"


def test_without_the_mode_ready_nodes_of_a_plain_graph_still_wait_for_each_other(atom, target, build_plan) -> None:
    registry = _registry(atom)
    result, node_args = build_plan(registry, target("DONE"))

    run = GraphExecutor(registry=registry).run(result.graph, node_args, run_id="r2")

    assert not run.succeeded, "порядок обычного графа по умолчанию не меняется: первый узел не дождался второго"
