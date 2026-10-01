"""The immutable run configuration and graph context (D5).

``GraphContext`` is what the engine threads to every node and router as its
opaque context object. It holds the interfaces and a frozen ``RunConfig``;
nodes reach the outside world only through these.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from new_core.graph import END
from new_core.loop.interfaces import (
    CancellationSignal,
    EventSink,
    LLMProvider,
    OwnerInbox,
    ToolRunner,
    TranscriptCompactor,
)


@dataclass(frozen=True)
class RunConfig:
    """Immutable per-run configuration.

    ``emergency_threshold`` is precomputed at setup. The original loop recomputed
    it on a model override, but ``get_model_context_tokens`` reads a static
    setting, so within a run the recompute is a no-op — the value is fixed here.
    """

    max_rounds: int
    model_context_tokens: int
    emergency_threshold: int
    emergency_use_chars: bool
    emergency_char_threshold: int
    fallback_model: str
    fallback_use_local: bool
    task_id: str
    task_type: str
    drive_root: pathlib.Path | None
    drive_logs: pathlib.Path
    max_retries: int
    light_model: str
    default_model: str
    default_effort: str
    default_use_local: bool

    #: The node a terminal outcome routes to (DA3). Defaults to the ``END``
    #: sentinel — the standalone ``run_llm_loop`` ends there, byte-for-byte as
    #: before. When the loop sub-topology is embedded in the task graph, the
    #: ouroboros side sets this to the task-level ``emit`` node so ``finalize`` /
    #: ``recover`` / ``round_limit_final`` route there instead of ending the run.
    terminal: str = END


@dataclass(frozen=True)
class GraphContext:
    """The six interfaces and the run configuration.

    Nodes reach the outside world only through this object, and only through an interface.
    A concrete ``ToolCatalog`` used to ride along beside them, because forming the prompt
    surface needed one and that work sat in ouroboros glue; ``ToolRunner.bind_surface`` took
    that over, so the catalog went back to being an implementation detail of the adapter
    that holds it.
    """

    llm: LLMProvider
    tools: ToolRunner
    compactor: TranscriptCompactor
    inbox: OwnerInbox
    events: EventSink
    config: RunConfig
    cancellation: CancellationSignal


__all__ = ["GraphContext", "RunConfig"]
