"""Execution semantics: nodes mutate state in place; edges thread the same object."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from new_core.graph import END, START, Graph, GraphExecutionError


def _record(name: str, order: list[str]) -> Callable[[dict[str, Any], object], None]:
    def node(state: dict[str, Any], ctx: object) -> None:
        order.append(name)

    return node


def test_static_edge_advances_to_fixed_successor() -> None:
    order: list[str] = []
    g: Graph[object, dict[str, Any]] = Graph()
    g.add_node("a", _record("a", order))
    g.add_node("b", _record("b", order))
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    g.build().invoke({}, ctx=None)
    assert order == ["a", "b"]


def test_conditional_edge_selects_router_choice() -> None:
    g: Graph[bool, dict[str, Any]] = Graph()
    g.add_node("a", lambda s, c: None)
    g.add_node("tools", lambda s, c: s.update({"went": "tools"}))
    g.add_node("final", lambda s, c: s.update({"went": "final"}))
    g.add_edge(START, "a")
    g.add_conditional_edge("a", lambda s, c: "tools" if c else "final")
    g.add_edge("tools", END)
    g.add_edge("final", END)
    cg = g.build()
    assert cg.invoke({}, ctx=True)["went"] == "tools"
    assert cg.invoke({}, ctx=False)["went"] == "final"


def test_router_may_direct_to_end() -> None:
    g: Graph[object, dict[str, Any]] = Graph()
    g.add_node("a", lambda s, c: s.update({"ran": True}))
    g.add_edge(START, "a")
    g.add_conditional_edge("a", lambda s, c: END)
    assert g.build().invoke({}, ctx=None) == {"ran": True}


def test_entry_begins_execution() -> None:
    order: list[str] = []
    g: Graph[object, dict[str, Any]] = Graph()
    g.add_node("entry", _record("entry", order))
    g.add_edge(START, "entry")
    g.add_edge("entry", END)
    g.build().invoke({}, ctx=None)
    assert order == ["entry"]


def test_state_accumulates_across_multi_node_path() -> None:
    g: Graph[object, dict[str, Any]] = Graph()
    g.add_node("a", lambda s, c: s["log"].append("a"))
    g.add_node("b", lambda s, c: s["log"].append("b"))
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    assert g.build().invoke({"log": []}, ctx=None)["log"] == ["a", "b"]


def test_router_observes_the_mutation_the_node_just_made() -> None:
    g: Graph[object, dict[str, Any]] = Graph()
    g.add_node("a", lambda s, c: s.update({"flag": "go"}))
    g.add_node("b", lambda s, c: s.update({"reached_b": True}))
    g.add_edge(START, "a")
    g.add_conditional_edge("a", lambda s, c: "b" if s.get("flag") == "go" else END)
    g.add_edge("b", END)
    assert g.build().invoke({}, ctx=None).get("reached_b") is True


def test_self_loop_repeats_until_router_exits_to_end() -> None:
    def tick(state: dict[str, Any], ctx: object) -> None:
        state["n"] += 1

    g: Graph[object, dict[str, Any]] = Graph()
    g.add_node("tick", tick)
    g.add_edge(START, "tick")
    g.add_conditional_edge("tick", lambda s, c: "tick" if s["n"] < 3 else END)
    assert g.build().invoke({"n": 0}, ctx=None)["n"] == 3


def test_node_that_mutates_nothing_leaves_state_unchanged() -> None:
    g: Graph[object, dict[str, Any]] = Graph()
    g.add_node("a", lambda s, c: None)
    g.add_edge(START, "a")
    g.add_edge("a", END)
    assert g.build().invoke({"x": 1}, ctx=None) == {"x": 1}


def test_invoke_mutates_and_returns_the_same_object() -> None:
    g: Graph[object, dict[str, Any]] = Graph()
    g.add_node("a", lambda s, c: s.update({"log": "a"}))
    g.add_edge(START, "a")
    g.add_edge("a", END)
    initial: dict[str, Any] = {}
    out = g.build().invoke(initial, ctx=None)
    assert out is initial
    assert initial == {"log": "a"}


def test_router_returning_unknown_node_raises_at_runtime() -> None:
    g: Graph[object, dict[str, Any]] = Graph()
    g.add_node("a", lambda s, c: None)
    g.add_edge(START, "a")
    g.add_conditional_edge("a", lambda s, c: "ghost")
    with pytest.raises(GraphExecutionError, match="ghost"):
        g.build().invoke({}, ctx=None)
