"""The per-task tool execution context, and the browser state it carries.

Lives here because every provider and gate needs it, and because the module it used to
share with ``ToolRegistry`` is gone. Skill code imports it from ``new_core.tools``.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# ``interfaces`` names ``ToolContext`` only under ``TYPE_CHECKING``, so importing it here at
# runtime closes no cycle — and ``events`` needs a real class for its default factory.
from new_core.tools.interfaces import NullToolEvents, ToolEvents, ToolSurface
from ouroboros.contracts.task_constraint import TaskConstraint
from ouroboros.utils import safe_relpath

__all__ = ["BrowserState", "ToolContext"]


@dataclass
class BrowserState:
    """Per-task browser state for optional browser tools."""

    pw_instance: Any | None = None
    # Legacy attribute kept for external code that may still inspect the old
    # Playwright handle name.
    playwright: Any | None = None
    browser: Any | None = None
    page: Any | None = None
    last_screenshot_b64: str = ""
    _thread_id: int | None = None
    _thread: Any | None = None
    _executor: Any | None = None


@dataclass
class ToolContext:
    """Tool execution context passed from the agent."""

    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    pending_events: list[dict[str, Any]] = field(default_factory=list)
    current_chat_id: int | str | None = None
    current_task_type: str | None = None
    current_task_source: str = ""
    transport: dict[str, Any] = field(default_factory=dict)
    #: Media delivery, keyed by kind ("file", "image"). Each sender returns
    #: ``(ok, detail)``; ``detail`` is ``DELIVERY_UNKNOWN`` when the transport outlived the
    #: wait and the remote outcome is genuinely ambiguous. Supplied by the supervisor when
    #: the agent is built, so a tool sends synchronously instead of queueing and hoping.
    media_senders: dict[str, Callable[..., tuple[bool, str]]] = field(default_factory=dict)
    pending_restart_reason: str | None = None
    last_push_succeeded: bool = False
    emit_progress_fn: Callable[[str], None] = field(default=lambda _: None)

    # How this task's tools report outward: usage, live logs, lifecycle events. Built by the
    # agent while assembling the context; the default emits nothing, for a context that has no
    # supervisor behind it. Never a queue — see ``ToolEvents``.
    events: ToolEvents = field(default_factory=NullToolEvents)

    # Budget tracking for usage events.
    task_id: str | None = None
    root_task_id: str | None = None
    conversation_id: str = ""
    trace_id: str = ""

    # Conversation messages for safety checks.
    messages: list[dict[str, Any]] | None = None

    # Attachments explicitly bound to the current task. Metadata only; no bytes or URLs.
    attachments: list[dict[str, Any]] = field(default_factory=list)

    # Structured task constraints, e.g. skill repair payload confinement.
    task_constraint: TaskConstraint | None = None

    # Task depth for fork-bomb protection.
    task_depth: int = 0

    # True inside handle_chat_direct, not a queued worker task.
    is_direct_chat: bool = False

    # Stable namespace for managed memory.
    memory_owner_id: str = "global"

    # Optional source timestamp for memory_save (e.g. distillation from source task).
    memory_recorded_at: float | None = None

    # Per-task isolated temp dir; created by task pipeline, cleaned up on task exit.
    task_workspace: pathlib.Path | None = None

    # Agent loop compaction / skill finalization state.
    _pending_compaction: int | None = None
    _skill_finalization_injected: bool = False

    # Optional browser tools state.
    browser_state: BrowserState = field(default_factory=BrowserState)

    # Read access to the tool catalog plus the surface-change intent channel. Set by the
    # `prepare_tools` node. Never a way for one tool to invoke another.
    tool_surface: ToolSurface | None = None

    def repo_path(self, rel: str) -> pathlib.Path:
        resolved = (self.repo_dir / safe_relpath(rel)).resolve()
        try:
            resolved.relative_to(self.repo_dir.resolve())
        except ValueError:
            raise ValueError(f"Path escapes repo_dir boundary: {rel}")
        return resolved

    def drive_path(self, rel: str) -> pathlib.Path:
        resolved = (self.drive_root / safe_relpath(rel)).resolve()
        try:
            resolved.relative_to(self.drive_root.resolve())
        except ValueError:
            raise ValueError(f"Path escapes drive_root boundary: {rel}")
        return resolved

    def drive_logs(self) -> pathlib.Path:
        return (self.drive_root / "logs").resolve()
