"""Sanity-check the mutation model against the real agent State buckets.

The loop's mutable state has three shapes of accumulation:
- ``messages`` — a growing transcript (append)
- ``usage``    — token counters (sum)
- overrides/flags (``model``, ``round``, ``final``) — last write wins

With in-place mutation the accumulation logic lives inside each node
(``state["messages"].append(...)``, ``state["usage"]["tokens"] += ...``) — no
reducer registry needed. This drives a mini agent-shaped graph to confirm the
three compose in one run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from new_core.graph import END, START, Graph


@dataclass(frozen=True)
class Ctx:
    """Stands in for the immutable provider bag (llm, tools, repos, ...)."""

    max_rounds: int


def test_agent_shaped_state_mutation_composes() -> None:
    g: Graph[Ctx, dict[str, Any]] = Graph()

    def guard(state: dict[str, Any], ctx: Ctx) -> None:
        state["round"] = state.get("round", 0) + 1

    def llm_call(state: dict[str, Any], ctx: Ctx) -> None:
        if state["round"] == 1:  # first round: ask for a tool
            state["messages"].append({"role": "assistant", "tool_calls": ["t"]})
            state["usage"]["tokens"] += 100
            state["model"] = "primary"
        else:  # second round: final answer
            state["messages"].append({"role": "assistant", "content": "done"})
            state["usage"]["tokens"] += 50
            state["final"] = "done"

    def exec_tools(state: dict[str, Any], ctx: Ctx) -> None:
        state["messages"].append({"role": "tool", "content": "result"})
        state["usage"]["tokens"] += 20

    g.add_node("guard", guard)
    g.add_node("llm", llm_call)
    g.add_node("tools", exec_tools)
    g.add_edge(START, "guard")
    g.add_edge("guard", "llm")
    g.add_conditional_edge("llm", lambda s, c: "tools" if s.get("final") is None else END)
    g.add_edge("tools", "guard")

    state: dict[str, Any] = {"messages": [{"role": "user", "content": "hi"}], "usage": {"tokens": 0}}
    out = g.build().invoke(state, ctx=Ctx(max_rounds=200), max_steps=50)

    assert out is state  # same object, mutated in place
    # messages: appended across rounds, order preserved
    assert [m["role"] for m in out["messages"]] == ["user", "assistant", "tool", "assistant"]
    # usage: counters summed
    assert out["usage"]["tokens"] == 170
    # last-write-wins channels
    assert out["model"] == "primary"
    assert out["round"] == 2
    assert out["final"] == "done"
