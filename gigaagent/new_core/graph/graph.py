"""The graph builder: declare nodes and edges, then ``build()``."""

from __future__ import annotations

from typing import Generic

from new_core.graph.built import BuiltGraph
from new_core.graph.errors import GraphValidationError
from new_core.graph.sentinels import END, START
from new_core.graph.types import NodeFn, RouterFn, TContext, TState


class Graph(Generic[TContext, TState]):
    """Builds a generic, dependency-free execution graph.

    A node is ``fn(state, ctx)`` that mutates ``state`` in place. Topology is
    data: each node has exactly one outgoing rule — either a single static edge
    (:meth:`add_edge`) or a single router (:meth:`add_conditional_edge`). Nodes
    never name their own successors. Call :meth:`build` to validate and get a
    runnable :class:`~new_core.graph.built.BuiltGraph`.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, NodeFn[TState, TContext]] = {}
        self._static_edges: dict[str, str] = {}
        self._routers: dict[str, RouterFn[TState, TContext]] = {}
        self._entry: str | None = None

    def add_node(self, name: str, fn: NodeFn[TState, TContext]) -> None:
        """Register a node under ``name``. Names must be unique and not reserved."""
        # Value equality (not identity): a plain ``str`` "START"/"END" equals the
        # marker and must be rejected too, or it would register as an ordinary node
        # that the identity-based terminal checks never stop on (it would execute).
        if name in (START, END):
            raise GraphValidationError(f"{name!r} is reserved and cannot be a node name")
        if name in self._nodes:
            raise GraphValidationError(f"node {name!r} is already defined")
        self._nodes[name] = fn

    def add_edge(self, src: str, dst: str) -> None:
        """Add a static edge ``src -> dst``.

        ``add_edge(START, "entry")`` declares the entry node. Otherwise ``src``
        is a node name and ``dst`` a node name or ``END``.
        """
        if src is START:
            if dst is START or dst is END:
                raise GraphValidationError("START must connect to a node, not a reserved marker")
            if self._entry is not None:
                raise GraphValidationError("entry node is already declared")
            self._entry = dst
            return
        self._reject_reserved_source(src)
        self._reject_existing_rule(src)
        self._static_edges[src] = dst

    def add_conditional_edge(self, src: str, router: RouterFn[TState, TContext]) -> None:
        """Route ``src``'s successor at runtime via ``router(state, ctx) -> name``."""
        self._reject_reserved_source(src)
        self._reject_existing_rule(src)
        self._routers[src] = router

    def build(self) -> BuiltGraph[TContext, TState]:
        """Validate the topology and return a runnable :class:`BuiltGraph`."""
        entry = self._validate()
        return BuiltGraph(
            nodes=dict(self._nodes),
            static_edges=dict(self._static_edges),
            routers=dict(self._routers),
            entry=entry,
        )

    def _reject_reserved_source(self, src: str) -> None:
        # Value equality, matching add_node: a plain "START"/"END" string cannot be
        # an edge source either (the entry-declaration path above uses identity so
        # only the real START object declares an entry).
        if src in (START, END):
            raise GraphValidationError(f"{src!r} cannot be an edge source")

    def _reject_existing_rule(self, src: str) -> None:
        if src in self._static_edges or src in self._routers:
            raise GraphValidationError(f"node {src!r} already has an outgoing edge")

    def _validate(self) -> str:
        """Validate the topology and return the (now guaranteed) entry node."""
        if not self._nodes:
            raise GraphValidationError("graph has no nodes")
        entry = self._entry
        if entry is None:
            raise GraphValidationError("no entry node set (use add_edge(START, ...))")
        if entry not in self._nodes:
            raise GraphValidationError(f"entry node {entry!r} is not a registered node")

        for src, dst in self._static_edges.items():
            if dst is END:
                continue
            if dst is START:
                raise GraphValidationError(f"edge {src!r} -> {dst!r}: START is not a valid destination")
            if dst not in self._nodes:
                raise GraphValidationError(f"edge {src!r} -> {dst!r}: destination is not a registered node")

        for name in self._nodes:
            if name not in self._static_edges and name not in self._routers:
                raise GraphValidationError(f"node {name!r} has no outgoing edge; add an edge to another node or to END")

        return entry
