"""A generic, dependency-free graph-execution core.

Model a control flow as nodes that thread a mutable state object between them:

- A **node** is ``fn(state, ctx)`` that mutates ``state`` in place.
- **Context** is an immutable, opaque per-run object (providers, metadata).
- Topology is data: static edges and conditional (router-based) edges.
- ``build()`` validates the topology; ``invoke`` then runs from the entry node
  until ``END`` and returns the same state object, mutated. The engine never
  inspects or copies state.

The core depends only on the Python standard library and imports nothing from
any application package.
"""

from __future__ import annotations

from new_core.graph.built import DEFAULT_MAX_STEPS, BuiltGraph
from new_core.graph.errors import (
    GraphError,
    GraphExecutionError,
    GraphValidationError,
    StepBudgetExceededError,
)
from new_core.graph.graph import Graph
from new_core.graph.sentinels import END, START
from new_core.graph.types import (
    NodeFn,
    RouterFn,
    TContext,
    TState,
)

__all__ = [
    "DEFAULT_MAX_STEPS",
    "END",
    "START",
    "BuiltGraph",
    "Graph",
    "GraphError",
    "GraphExecutionError",
    "GraphValidationError",
    "NodeFn",
    "RouterFn",
    "StepBudgetExceededError",
    "TContext",
    "TState",
]
