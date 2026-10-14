"""Step budget: the engine caps node executions independently of END termination."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Any

import pytest

from new_core.graph import END, START, BuiltGraph, Graph, StepBudgetExceededError
from tests.helpers.parametrize import parametrize_from_dict


def _make_infinite_loop() -> BuiltGraph[object, dict[str, Any]]:
    g: Graph[object, dict[str, Any]] = Graph()
    g.add_node("loop", lambda s, c: None)
    g.add_edge(START, "loop")
    g.add_conditional_edge("loop", lambda s, c: "loop")  # never reaches END
    return g.build()


def _tick(state: dict[str, Any], ctx: object) -> None:
    state["n"] += 1


def _make_counter() -> BuiltGraph[object, dict[str, Any]]:
    g: Graph[object, dict[str, Any]] = Graph()
    g.add_node("tick", _tick)
    g.add_edge(START, "tick")
    g.add_conditional_edge("tick", lambda s, c: "tick" if s["n"] < 3 else END)
    return g.build()


_INFINITE_LOOP = _make_infinite_loop()
_COUNTER = _make_counter()


@parametrize_from_dict(
    {
        "exceeds budget on an infinite loop": {
            "graph": _INFINITE_LOOP,
            "state": {},
            "max_steps": 10,
            "expected_context": pytest.raises(StepBudgetExceededError, match="step budget"),
        },
        "completes within budget": {
            "graph": _COUNTER,
            "state": {"n": 0},
            "max_steps": 10,
            "expected_context": nullcontext(),
        },
        "rejects a non-positive budget": {
            "graph": _COUNTER,
            "state": {"n": 0},
            "max_steps": 0,
            "expected_context": pytest.raises(ValueError, match="max_steps"),
        },
    }
)
def test_step_budget(
    graph: BuiltGraph[object, dict[str, Any]],
    state: dict[str, Any],
    max_steps: int,
    expected_context: AbstractContextManager[object],
) -> None:
    with expected_context:
        graph.invoke(state, ctx=None, max_steps=max_steps)
