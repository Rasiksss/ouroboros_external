"""The per-task tool execution context, and the browser state it carries.

Lives here because every provider and gate needs it, and because the module it used to
share with ``ToolRegistry`` is gone. Skill code imports it from ``new_core.tools``.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

# ``interfaces`` names ``ToolContext`` only under ``TYPE_CHECKING``, so importing it here at
# runtime closes no cycle — and ``events`` needs a real class for its default factory.
from new_core.identity import OwnerScope
from new_core.tools.interfaces import NullToolEvents, ToolEvents, ToolOutbound, ToolSurface
from ouroboros.utils import safe_relpath

if TYPE_CHECKING:
    from new_core.prompt import RenderedSection
    from new_core.skills.interfaces import IExtensionRuntime
    from new_core.skills.surface import SkillSurface
    from ouroboros.contracts.attachments import AttachmentMetadata, TerminalAttachment
    from ouroboros.egress.workstation import WorkstationHandle
    from ouroboros.tools.skills import SkillReadResult

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
    #: Whose data this task touches. The one address a tool has: owner, surface, chat.
    scope: OwnerScope
    skill_surface: SkillSurface | None
    extension_runtime: IExtensionRuntime | None = None
    #: What this run reports outward when it ends. Named messages, never dicts: the
    #: engine cannot name the kinds, so the list is typed by what the run publishes.
    pending_events: list[Any] = field(default_factory=list)
    current_task_type: str | None = None
    transport: dict[str, Any] = field(default_factory=dict)
    #: How this run hands the owner a file. One boundary handed over by the composition
    #: root that built the run, never a pair of callables captured from another component:
    #: what changes when the run moves to its own process is the outbox behind it, and
    #: nothing in the run, the tools or the boundary itself. ``None`` when nothing composed
    #: one, and a tool then reports the attachment transport unavailable.
    outbound: ToolOutbound | None = None
    last_push_succeeded: bool = False
    emit_progress_fn: Callable[[str], None] = field(default=lambda _: None)
    #: The current run's cooperative stop, bound by the tool executor before dispatch.
    stop_requested: Callable[[], bool] = field(default=lambda: False)

    # How this task's tools report outward: usage, live logs, lifecycle events. Built by the
    # agent while assembling the context; the default emits nothing, for a context that has no
    # supervisor behind it. Never a queue — see ``ToolEvents``.
    events: ToolEvents = field(default_factory=NullToolEvents)

    task_id: str | None = None
    job_id: str = ""
    detached: bool = False
    execution_claim: str = ""
    tool_call_id: str = ""
    inherited_context_seed: str = ""
    committed_summary_snapshot: str = ""
    through_message_id: str = ""
    root_task_id: str | None = None
    trace_id: str = ""

    messages: list[dict[str, Any]] | None = None
    light_model: str = ""
    skill_origins: frozenset[str] | None = None

    # Attachments explicitly bound to the current task. Metadata only; no bytes or URLs.
    attachments: list[AttachmentMetadata] = field(default_factory=list)
    terminal_attachments: list[TerminalAttachment] = field(default_factory=list)

    # Task depth for fork-bomb protection.
    task_depth: int = 0

    # Optional source timestamp for memory_save (e.g. distillation from source task).
    memory_recorded_at: float | None = None

    # Per-task isolated temp dir; created by task pipeline, cleaned up on task exit.
    task_workspace: pathlib.Path | None = None

    _pending_compaction: int | None = None
    active_skill_pages: dict[str, list[SkillReadResult]] = field(default_factory=dict)
    skill_recency: list[str] = field(default_factory=list)
    projected_skills: set[str] = field(default_factory=set)
    pending_skill_read: str | None = None
    prompt_sections: list[RenderedSection] = field(default_factory=list)
    prompt_soft_cap: int = 0

    browser_state: BrowserState = field(default_factory=BrowserState)

    # Read access to the tool catalog plus the surface-change intent channel. Set by the
    # `prepare_tools` node. Never a way for one tool to invoke another.
    tool_surface: ToolSurface | None = None

    # This turn's AEF WorkStation session, or None when the integration is off or
    # unreachable. Presence is what puts the five workstation tools on the surface
    # (workstation spec §19.4); set once by `_prepare_task_context`, before `prepare_tools`.
    workstation: WorkstationHandle | None = None

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
