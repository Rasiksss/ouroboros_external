"""Assemble the loop graph from nodes and routers (topology as data, D6).

START → guard
guard            ─(router)─► round_limit_final | apply_overrides
apply_overrides ─► drain_inbox ─► checkpoint ─► prepare_transcript ─► llm_call
llm_call         ─(router)─► recover | exec_tools | finalize
recover          ─(router)─► «terminal» | exec_tools | finalize
exec_tools       ─► guard
finalize         ─(router)─► guard | «terminal»
round_limit_final ─(router)─► «terminal»

The three terminal outcomes (``recover`` exhausted, ``finalize`` done,
``round_limit_final``) route to the node named by ``ctx.config.terminal`` (DA3):
``END`` for the standalone loop and the task graph's ``emit`` node when embedded.
``register_loop_topology`` authors this sub-topology once so both
``build_loop_graph`` and the ouroboros ``build_task_graph`` reuse it.
"""

from __future__ import annotations

from new_core.graph import START, BuiltGraph, Graph
from new_core.loop import nodes, routers
from new_core.loop.context import GraphContext
from new_core.loop.state import RunState

#: Node executions per round (guard, apply_overrides, drain_inbox, checkpoint,
#: prepare_transcript, llm_call, [recover], exec_tools ≈ 8). ``max_steps`` is a
#: pure anti-infinite-loop backstop, kept distinct from — and well above — the
#: ``MAX_ROUNDS`` domain edge, which the guard router enforces (Task 4.2).
_STEPS_PER_ROUND = 12


def max_steps_for(max_rounds: int) -> int:
    """A generous step budget derived from ``max_rounds`` (backstop only)."""
    return max_rounds * _STEPS_PER_ROUND + 100


def register_loop_topology(graph: Graph[GraphContext, RunState]) -> None:
    """Register the loop nodes and their internal edges onto ``graph`` (DA3).

    Authors the loop sub-topology once — every node from ``guard`` through
    ``round_limit_final`` and every edge/router *between* them — but not the
    ``START`` entry edge, which each embedding graph supplies (``build_loop_graph``
    enters at ``guard``; the task graph enters at ``prepare_context``). The three
    terminal routers return ``ctx.config.terminal``, so the same registration
    serves both the standalone loop (terminal ``END``) and the task graph
    (terminal ``emit``).
    """
    graph.add_node("guard", nodes.guard)
    graph.add_node("apply_overrides", nodes.apply_overrides)
    graph.add_node("drain_inbox", nodes.drain_inbox)
    graph.add_node("checkpoint", nodes.checkpoint)
    graph.add_node("prepare_transcript", nodes.prepare_transcript)
    graph.add_node("llm_call", nodes.llm_call)
    graph.add_node("recover", nodes.recover)
    graph.add_node("exec_tools", nodes.exec_tools)
    graph.add_node("finalize", nodes.finalize)
    graph.add_node("round_limit_final", nodes.round_limit_final)

    graph.add_conditional_edge("guard", routers.route_guard)
    graph.add_edge("apply_overrides", "drain_inbox")
    graph.add_edge("drain_inbox", "checkpoint")
    graph.add_edge("checkpoint", "prepare_transcript")
    graph.add_edge("prepare_transcript", "llm_call")
    graph.add_conditional_edge("llm_call", routers.route_after_llm)
    graph.add_conditional_edge("recover", routers.route_after_recover)
    graph.add_edge("exec_tools", "guard")
    graph.add_conditional_edge("finalize", routers.route_after_finalize)
    graph.add_conditional_edge("round_limit_final", routers.route_after_round_limit)


def build_loop_graph() -> BuiltGraph[GraphContext, RunState]:
    """Register the loop sub-topology, enter at ``guard``, and build (terminal ``END``)."""
    graph: Graph[GraphContext, RunState] = Graph()
    register_loop_topology(graph)
    graph.add_edge(START, "guard")
    return graph.build()


#: Process-wide shared instance, built once on first use by ``get_loop_graph``.
_loop_graph: BuiltGraph[GraphContext, RunState] | None = None


def get_loop_graph() -> BuiltGraph[GraphContext, RunState]:
    """Return the shared loop graph, building it once on first use.

    The built graph is topology-only and stateless — nodes and routers are pure
    functions, and every piece of mutable run data lives in the ``RunState`` /
    ``GraphContext`` passed to ``invoke`` — so one instance is safely reused across
    tasks and threads. Callers build once (at agent construction) and then only
    ``invoke``, instead of reassembling the graph on every run.
    """
    global _loop_graph
    if _loop_graph is None:
        _loop_graph = build_loop_graph()
    return _loop_graph


__all__ = ["build_loop_graph", "get_loop_graph", "max_steps_for"]
