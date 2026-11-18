"""Assemble the graphs from nodes and routers (topology as data, D6).

The task graph, ``build_task_graph``:

START → record_task_started → prepare_context → resume
resume           ─(router)─► «terminal» | confirm | open_transcript
open_transcript ─► prepare_tools ─► guard
guard            ─(router)─► round_limit_final | apply_overrides
apply_overrides ─► drain_inbox ─► checkpoint ─► prepare_transcript ─► llm_call
llm_call         ─(router)─► recover | confirm | finalize
recover          ─(router)─► «terminal» | confirm | finalize
confirm          ─(router)─► «terminal» (parked on the owner) | exec_tools
exec_tools       ─► guard
finalize         ─(router)─► «terminal»
round_limit_final ─(router)─► «terminal»
«terminal»       ─► emit ─► END

The terminal outcomes route to the node named by ``ctx.config.terminal`` (DA3). The round, from
``guard`` down, is ``register_loop_topology``; ``build_loop_graph`` enters the same round at ``guard``
with a formed state and terminal ``END``, and only tests use it.
"""

from __future__ import annotations

from new_core.graph import END, START, BuiltGraph, Graph
from new_core.loop import nodes, routers
from new_core.loop.context import GraphContext
from new_core.loop.state import RunState

#: Node executions per round (guard, apply_overrides, drain_inbox, checkpoint,
#: prepare_transcript, llm_call, [recover], confirm, exec_tools ≈ 9). ``max_steps`` is a
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
    enters at ``guard``; the task graph enters at ``record_task_started``). The three
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
    graph.add_node("confirm", nodes.confirm)
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
    graph.add_conditional_edge("confirm", routers.route_after_confirm)
    graph.add_edge("exec_tools", "guard")
    graph.add_conditional_edge("finalize", routers.route_after_finalize)
    graph.add_conditional_edge("round_limit_final", routers.route_after_round_limit)


def build_loop_graph() -> BuiltGraph[GraphContext, RunState]:
    """Register the loop sub-topology, enter at ``guard``, and build (terminal ``END``)."""
    graph: Graph[GraphContext, RunState] = Graph()
    register_loop_topology(graph)
    graph.add_edge(START, "guard")
    return graph.build()


def build_task_graph() -> BuiltGraph[GraphContext, RunState]:
    """The whole task graph: the head, the round and the terminal ``emit``."""
    graph: Graph[GraphContext, RunState] = Graph()
    graph.add_node("record_task_started", nodes.record_task_started)
    graph.add_node("prepare_context", nodes.prepare_context)
    graph.add_node("resume", nodes.resume)
    graph.add_node("open_transcript", nodes.open_transcript)
    graph.add_node("prepare_tools", nodes.prepare_tools)
    register_loop_topology(graph)
    graph.add_node("emit", nodes.emit)

    graph.add_edge(START, "record_task_started")
    graph.add_edge("record_task_started", "prepare_context")
    graph.add_edge("prepare_context", "resume")
    graph.add_conditional_edge("resume", routers.route_after_resume)
    graph.add_edge("open_transcript", "prepare_tools")
    graph.add_edge("prepare_tools", "guard")
    graph.add_edge("emit", END)
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


__all__ = ["build_loop_graph", "build_task_graph", "get_loop_graph", "max_steps_for"]
