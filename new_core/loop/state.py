"""The mutable run state threaded through the loop graph (D5).

Nodes mutate ``RunState`` in place; the engine never inspects or copies it. It
holds only loop-native data — the tool-coupled state (``browser_state``) stays
on ``ToolContext`` behind the interfaces (D4). The return triple
``(final_content, accumulated_usage, llm_trace)`` is read back off this object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from new_core.identity import OwnerScope
from new_core.llm.types import LLMFailure, LLMMessage
from new_core.loop.context import RunConfig
from new_core.loop.interfaces import ConfirmationKey, LoopOverrides

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

    #: Copied from ``RunConfig`` by ``make_run_state``; the one identity every node,
    #: tool context and lifecycle path reads.
    scope: OwnerScope

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
    last_failure: LLMFailure | None = None
    pending_compaction: int | None = None
    pending_overrides: LoopOverrides = field(default_factory=LoopOverrides)
    checkpoint_injected: bool = False
    owner_msg_seen: set[str] = field(default_factory=set)

    suspended: bool = False
    resumed: bool = False
    confirmation_decisions: dict[ConfirmationKey, str] = field(default_factory=dict)

    #: ``None`` until a terminal node records the final answer.
    final_content: str | None = None

    #: Opaque task-level fields (DA6), the same opaque-value tactic as ``usage`` /
    #: ``llm_trace`` / ``final_content`` (D2). The ``new_core/loop`` layer never inspects
    #: them — it cannot import their ouroboros types. ``task`` / ``task_workspace`` are
    #: the inputs ``prepare_context`` hands to the context assembler; ``tool_ctx`` is
    #: what it writes back (later read by ``prepare_tools``, the ``emit`` node and the
    #: crash path). All default so the standalone ``run_llm_loop``, which never touches
    #: them, is unaffected.
    task: Any = None
    task_workspace: Any = None
    #: ``None`` until ``prepare_context`` produces it; every node downstream of that
    #: edge has one. The other two stay ``Any`` — no consumer needs them typed.
    tool_ctx: CtxT | None = None

    def snapshot(self) -> dict[str, Any]:
        """What a parked run needs to pick the round back up, in JSON-storable form.

        ``tool_schemas`` stay out: a schema is an object, not data, and ``resume`` asks for them by name.
        """
        return {
            "messages": self.messages,
            "active_tool_names": sorted(self.active_tool_names),
            "active_model": self.active_model,
            "active_effort": self.active_effort,
            "active_use_local": self.active_use_local,
            "accumulated_usage": self.accumulated_usage,
            "llm_trace": self.llm_trace,
            "round_idx": self.round_idx,
            "last_resp": None if self.last_resp is None else self.last_resp.to_dict(),
            "pending_compaction": self.pending_compaction,
            "checkpoint_injected": self.checkpoint_injected,
            "owner_msg_seen": sorted(self.owner_msg_seen),
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        """Refill this state from :meth:`snapshot`, in place: the run handed these objects out already.

        A field the snapshot does not carry keeps its built value, since a rollout runs two versions.
        """
        stored = {**self.snapshot(), **snapshot}
        self.messages[:] = stored["messages"]
        self.active_tool_names.clear()
        self.active_tool_names.update(stored["active_tool_names"])
        self.accumulated_usage.clear()
        self.accumulated_usage.update(stored["accumulated_usage"])
        self.llm_trace.clear()
        self.llm_trace.update(stored["llm_trace"])
        self.owner_msg_seen.clear()
        self.owner_msg_seen.update(stored["owner_msg_seen"])
        self.active_model = stored["active_model"]
        self.active_effort = stored["active_effort"]
        self.active_use_local = stored["active_use_local"]
        self.round_idx = stored["round_idx"]
        last_resp = stored["last_resp"]
        self.last_resp = None if last_resp is None else LLMMessage.from_dict(last_resp)
        self.pending_compaction = stored["pending_compaction"]
        self.checkpoint_injected = stored["checkpoint_injected"]

    def record_llm_result(self, result: LLMMessage | LLMFailure) -> None:
        """Replace both sides of the latest call outcome together."""
        self.last_resp, self.last_failure = (None, result) if isinstance(result, LLMFailure) else (result, None)


def make_run_state(
    config: RunConfig,
    *,
    messages: list[dict[str, Any]],
    tool_schemas: list[Any],
    active_tool_names: set[str],
) -> RunState[Any]:
    """Build the initial ``RunState``, seeding the task id, scope and active model/effort/use-local from config."""
    return RunState(
        task_id=config.task_id,
        scope=config.scope,
        messages=messages,
        tool_schemas=tool_schemas,
        active_tool_names=active_tool_names,
        active_model=config.default_model,
        active_effort=config.default_effort,
        active_use_local=config.default_use_local,
    )


__all__ = ["CtxT", "RunState", "make_run_state"]
