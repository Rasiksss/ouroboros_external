"""Graph construction and build-time validation."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

import pytest

from new_core.graph import END, START, Graph, GraphValidationError
from tests.custom_parametrize import parametrize_from_dict


def _noop(state: dict[str, Any], ctx: object) -> None:
    return None


def _to_end(state: dict[str, Any], ctx: object) -> str:
    return END


@parametrize_from_dict(
    {
        "valid graph": {
            "nodes": ["a"],
            "edges": [(START, "a"), ("a", END)],
            "cond_edges": [],
            "expected_context": nullcontext(),
        },
        "edge to unknown node": {
            "nodes": ["a"],
            "edges": [(START, "a"), ("a", "ghost")],
            "cond_edges": [],
            "expected_context": pytest.raises(GraphValidationError, match="ghost"),
        },
        "missing entry": {
            "nodes": ["a"],
            "edges": [("a", END)],
            "cond_edges": [],
            "expected_context": pytest.raises(GraphValidationError, match="entry"),
        },
        "unknown entry": {
            "nodes": ["a"],
            "edges": [(START, "ghost"), ("a", END)],
            "cond_edges": [],
            "expected_context": pytest.raises(GraphValidationError, match="ghost"),
        },
        "non-terminal dead end": {
            "nodes": ["a", "b"],
            "edges": [(START, "a"), ("a", "b")],
            "cond_edges": [],
            "expected_context": pytest.raises(GraphValidationError, match="outgoing edge"),
        },
        "empty graph": {
            "nodes": [],
            "edges": [],
            "cond_edges": [],
            "expected_context": pytest.raises(GraphValidationError, match="no nodes"),
        },
        "invalid sentinel destination": {
            "nodes": ["a"],
            "edges": [(START, "a"), ("a", START)],
            "cond_edges": [],
            "expected_context": pytest.raises(GraphValidationError, match="destination"),
        },
        "reserved node name": {
            "nodes": [END],
            "edges": [],
            "cond_edges": [],
            "expected_context": pytest.raises(GraphValidationError, match="reserved"),
        },
        # Value-collision guard: a plain "END"/"START" str equals the marker, so it
        # must be rejected too — otherwise it registers as an ordinary node that the
        # identity-based terminal checks never stop on, and it executes.
        "reserved node name as plain string": {
            "nodes": ["END"],
            "edges": [],
            "cond_edges": [],
            "expected_context": pytest.raises(GraphValidationError, match="reserved"),
        },
        "reserved plain-string edge source": {
            "nodes": ["a"],
            "edges": [("END", "a")],
            "cond_edges": [],
            "expected_context": pytest.raises(GraphValidationError, match="edge source"),
        },
        "duplicate node": {
            "nodes": ["a", "a"],
            "edges": [],
            "cond_edges": [],
            "expected_context": pytest.raises(GraphValidationError, match="already defined"),
        },
        "second outgoing rule (static then conditional)": {
            "nodes": ["a"],
            "edges": [(START, "a"), ("a", END)],
            "cond_edges": [("a", _to_end)],
            "expected_context": pytest.raises(GraphValidationError, match="already has an outgoing edge"),
        },
        "START to END is rejected": {
            "nodes": ["a"],
            "edges": [(START, END)],
            "cond_edges": [],
            "expected_context": pytest.raises(GraphValidationError, match="reserved marker"),
        },
        "START to START is rejected": {
            "nodes": ["a"],
            "edges": [(START, START)],
            "cond_edges": [],
            "expected_context": pytest.raises(GraphValidationError, match="reserved marker"),
        },
        "entry declared twice is rejected": {
            "nodes": ["a", "b"],
            "edges": [(START, "a"), (START, "b")],
            "cond_edges": [],
            "expected_context": pytest.raises(GraphValidationError, match="already declared"),
        },
        "reserved edge source is rejected": {
            "nodes": ["a"],
            "edges": [(END, "a")],
            "cond_edges": [],
            "expected_context": pytest.raises(GraphValidationError, match="edge source"),
        },
        "reserved conditional-edge source is rejected": {
            "nodes": ["a"],
            "edges": [],
            "cond_edges": [(START, _to_end)],
            "expected_context": pytest.raises(GraphValidationError, match="edge source"),
        },
    }
)
def test_build_validation(
    nodes: list[str],
    edges: list[tuple[str, str]],
    cond_edges: list[tuple[str, Callable[[Any, Any], str]]],
    expected_context: AbstractContextManager[object],
) -> None:
    # The context wraps the whole build so it catches both eager rejections
    # (raised inside an add_* call) and deferred ones (raised by build()).
    with expected_context:
        g: Graph[object, dict[str, Any]] = Graph()
        for name in nodes:
            g.add_node(name, _noop)
        for src, dst in edges:
            g.add_edge(src, dst)
        for src, router in cond_edges:
            g.add_conditional_edge(src, router)
        g.build()
