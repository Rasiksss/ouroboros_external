"""Type parameters and callable contracts for the graph core.

The engine is generic over two caller-defined types:

- ``TContext`` — the immutable per-run context (providers, task metadata). The
  engine treats it as fully opaque and only hands it to nodes and routers.
- ``TState`` — the mutable run state object. Nodes mutate it in place; the engine
  never inspects or copies it, only threads the same object through the graph.
  It has no structural bound: a caller picks its shape (a dict, a dataclass, ...).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

#: Immutable per-run context, opaque to the engine.
TContext = TypeVar("TContext")

#: Mutable run state, passed through and mutated in place by nodes.
TState = TypeVar("TState")

#: A node: ``fn(state, ctx)`` that mutates ``state`` in place. Its return is ignored.
NodeFn = Callable[[TState, TContext], None]

#: A router for a conditional edge: returns the next node name (a str), or ``END``.
RouterFn = Callable[[TState, TContext], str]
