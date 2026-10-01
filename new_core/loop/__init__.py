"""The ouroboros LLM-with-tools loop, expressed on the ``new_core`` graph engine.

Semantic phases are nodes; branching is routers; external effects go through a
fixed set of ABC interfaces held by an immutable ``GraphContext``. This layer
imports nothing from ``ouroboros`` (see the import-boundary test); concrete
adapters that implement the interfaces live in ``ouroboros/agent/``.
"""

from __future__ import annotations

from new_core.loop.build import (
    build_loop_graph,
    get_loop_graph,
    max_steps_for,
    register_loop_topology,
)
from new_core.loop.context import GraphContext, RunConfig
from new_core.loop.interfaces import (
    CancellationSignal,
    EventSink,
    LLMProvider,
    LoopOverrides,
    OwnerInbox,
    ToolRunner,
    TranscriptCompactor,
)
from new_core.loop.state import RunState, make_run_state

__all__ = [
    "CancellationSignal",
    "EventSink",
    "GraphContext",
    "LLMProvider",
    "LoopOverrides",
    "OwnerInbox",
    "RunConfig",
    "RunState",
    "ToolRunner",
    "TranscriptCompactor",
    "build_loop_graph",
    "get_loop_graph",
    "make_run_state",
    "max_steps_for",
    "register_loop_topology",
]
