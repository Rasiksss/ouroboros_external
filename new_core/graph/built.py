"""The built, runnable form of a graph."""

from __future__ import annotations

from typing import Generic

from new_core.graph.errors import GraphExecutionError, StepBudgetExceededError
from new_core.graph.sentinels import END, START
from new_core.graph.types import NodeFn, RouterFn, TContext, TState

#: Default cap on node executions per run; overridable per ``invoke`` call.
DEFAULT_MAX_STEPS = 1000


class BuiltGraph(Generic[TContext, TState]):
    """A validated graph ready to run. Construct via :meth:`Graph.build`."""

    def __init__(
        self,
        *,
        nodes: dict[str, NodeFn[TState, TContext]],
        static_edges: dict[str, str],
        routers: dict[str, RouterFn[TState, TContext]],
        entry: str,
    ) -> None:
        self._nodes = nodes
        self._static_edges = static_edges
        self._routers = routers
        self._entry = entry

    def invoke(self, state: TState, ctx: TContext, *, max_steps: int = DEFAULT_MAX_STEPS) -> TState:
        """Run from the entry node until END, returning the (same) state object.

        Each node mutates ``state`` in place before the next edge is followed, so
        routers observe the mutation the node just made. The passed-in ``state``
        object is mutated and returned as-is. Raises :class:`StepBudgetExceededError`
        if the run does not reach END within ``max_steps`` node executions.
        """
        if max_steps <= 0:
            raise ValueError("max_steps must be positive")

        current: str = self._entry
        steps = 0
        while current is not END:
            if steps >= max_steps:
                raise StepBudgetExceededError(f"run exceeded the step budget of {max_steps} steps without reaching END")
            if current is START:
                raise GraphExecutionError(f"cannot execute reserved marker {current!r}")
            node = self._nodes.get(current)
            if node is None:
                raise GraphExecutionError(f"no node named {current!r}")
            node(state, ctx)
            current = self._next(current, state, ctx)
            steps += 1
        return state

    def _next(self, current: str, state: TState, ctx: TContext) -> str:
        """Resolve the successor of ``current`` via its static edge or router."""
        static_dst = self._static_edges.get(current)
        if static_dst is not None:
            return static_dst
        router = self._routers.get(current)
        if router is None:  # pragma: no cover - build() guarantees every node has a rule
            raise GraphExecutionError(f"node {current!r} has no outgoing edge")
        dst = router(state, ctx)
        if dst is END:
            return END
        if dst is START:
            raise GraphExecutionError(f"router for {current!r} returned reserved marker {dst!r}")
        if dst not in self._nodes:
            raise GraphExecutionError(f"router for {current!r} returned unknown node {dst!r}")
        return dst
