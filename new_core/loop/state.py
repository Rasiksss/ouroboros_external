"""The mutable run state threaded through the loop graph (D5).

Nodes mutate ``RunState`` in place; the engine never inspects or copies it. It
holds only loop-native data — the tool-coupled state (``browser_state``) stays
on ``ToolContext`` behind the interfaces (D4). The return triple
``(final_content, accumulated_usage, llm_trace)`` is read back off this object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from new_core.llm.types import LLMMessage
from new_core.loop.context import RunConfig
from new_core.loop.interfaces import LoopOverrides

#: The tool context's type, supplied by the adapter layer. This package cannot name
#: ``ToolContext`` — it must not import ouroboros — so it takes the type as a parameter
#: instead of falling back to ``Any``. Nodes and routers here keep the unparametrized
#: ``RunState`` (i.e. ``RunState[Any]``): they pass the context across without ever
#: looking inside it. The ouroboros glue writes ``RunState[ToolContext]`` and gets a
#: checked field.
CtxT = TypeVar("CtxT")


@dataclass
class RunState(Generic[CtxT]):
    """Mutable state for one ``run_llm_loop`` invocation."""

    #: Which task this run belongs to. Lives here rather than inside an adapter so the
    #: effects behind the interfaces stay task-agnostic: a node hands the id across on
    #: the call that needs it, the same way it hands ``messages`` or ``tool_ctx``.
    task_id: str

    messages: list[dict[str, Any]]
    tool_schemas: list[Any]
    active_tool_names: set[str]

    active_model: str
    active_effort: str
    active_use_local: bool

    accumulated_usage: dict[str, Any] = field(default_factory=dict)
    llm_trace: dict[str, Any] = field(default_factory=lambda: {"reasoning_notes": [], "tool_calls": []})

    round_idx: int = 0
    last_resp: LLMMessage | None = None
    pending_compaction: int | None = None
    pending_overrides: LoopOverrides = field(default_factory=LoopOverrides)
    checkpoint_injected: bool = False
    skill_finalization_injected: bool = False
    owner_msg_seen: set[str] = field(default_factory=set)

    #: ``None`` until a terminal node records the final answer. The finalize
    #: router uses this sentinel to distinguish "reinject and loop" from "end".
    final_content: str | None = None

    #: Opaque task-level fields owned by the ouroboros glue nodes (DA6), the same
    #: opaque-value tactic as ``usage`` / ``llm_trace`` / ``final_content`` (D2).
    #: The ``new_core/loop`` layer never inspects them — it cannot import their
    #: ouroboros types. ``task`` / ``task_workspace`` are inputs the task graph's
    #: ``prepare_context`` node reads; ``tool_ctx`` / ``cap_info`` are what it
    #: writes (``tool_ctx`` is later read by ``prepare_tools``, the ``emit`` node
    #: and the crash path). All default so the standalone ``run_llm_loop``, which
    #: never touches them, is unaffected.
    task: Any = None
    task_workspace: Any = None
    #: ``None`` until ``prepare_context`` produces it; every node downstream of that
    #: edge has one. The remaining three stay ``Any`` — no consumer needs them typed.
    tool_ctx: CtxT | None = None
    cap_info: Any = None


def make_run_state(
    config: RunConfig,
    *,
    messages: list[dict[str, Any]],
    tool_schemas: list[Any],
    active_tool_names: set[str],
) -> RunState[Any]:
    """Build the initial ``RunState``, seeding the task id and active model/effort/use-local from config."""
    return RunState(
        task_id=config.task_id,
        messages=messages,
        tool_schemas=tool_schemas,
        active_tool_names=active_tool_names,
        active_model=config.default_model,
        active_effort=config.default_effort,
        active_use_local=config.default_use_local,
    )


__all__ = ["CtxT", "RunState", "make_run_state"]
