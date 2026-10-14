"""The loop graph is built once and reused; explicit builds stay independent, order is fixed."""

from __future__ import annotations

from new_core.graph import END, BuiltGraph
from new_core.loop import build_loop_graph, build_task_graph, get_loop_graph, routers


def test_get_loop_graph_returns_shared_instance() -> None:
    first = get_loop_graph()
    second = get_loop_graph()
    assert first is second
    assert isinstance(first, BuiltGraph)


def test_build_loop_graph_returns_fresh_instances() -> None:
    # The explicit builder always assembles a new graph; only get_loop_graph caches.
    assert build_loop_graph() is not build_loop_graph()
    assert build_loop_graph() is not get_loop_graph()


#: The unconditional spine of one round, in order. Conditional hops (guard, llm_call,
#: recover, finalize, round_limit_final) belong to the routers and are covered by
#: `test_nodes_routers`; these are the edges nothing else pins.
_ROUND_SPINE = [
    ("apply_overrides", "drain_inbox"),
    ("drain_inbox", "checkpoint"),
    ("checkpoint", "prepare_transcript"),
    ("prepare_transcript", "llm_call"),
    ("exec_tools", "guard"),
]


def test_the_round_spine_runs_in_this_order() -> None:
    """Order is behaviour, not layout.

    `drain_inbox` before `checkpoint` is why an owner message arriving mid-round is
    decided on the current transcript rather than one round late; `prepare_transcript`
    before `llm_call` is why compaction applies to the request that follows it. A
    characterization test used to pin the whole per-round effect sequence; when it was
    replaced, the assertion narrowed to three entries and these edges stopped being
    covered — swapping two of them left the suite green.
    """
    graph = build_loop_graph()

    assert graph._entry == "guard", "the standalone loop enters at the round guard"
    assert sorted(graph._static_edges.items()) == sorted(_ROUND_SPINE)


def test_the_task_graph_runs_its_head_into_the_round_and_ends_at_emit() -> None:
    graph = build_task_graph()

    assert graph._entry == "record_task_started"
    assert graph._static_edges["record_task_started"] == "prepare_context"
    assert graph._static_edges["prepare_context"] == "resume"
    assert graph._routers["resume"] is routers.route_after_resume
    assert graph._static_edges["open_transcript"] == "prepare_tools"
    assert graph._static_edges["prepare_tools"] == "guard"
    assert graph._static_edges["emit"] is END
