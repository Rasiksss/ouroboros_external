"""Minimal runtime-checkable ToolContext ABI for tools and extensions."""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    # Annotation only: the runtime protocol check reads annotation *names*, so this keeps the
    # contracts package free of a runtime dependency on the tool layer.
    from new_core.tools.interfaces import ToolEvents


@runtime_checkable
class ToolContextProtocol(Protocol):
    """Minimum ToolContext fields; extend only with a deliberate contract bump."""

    # Filesystem roots.
    repo_dir: pathlib.Path
    drive_root: pathlib.Path

    # Runtime drains pending_events; emit_progress_fn is best-effort.
    pending_events: list
    emit_progress_fn: Callable[[str], Any]

    # Outward reporting: usage, live logs, lifecycle events. An interface, never a queue —
    # a tool emits through it and never learns what carries the event.
    events: ToolEvents

    # May be None outside a running task.
    current_chat_id: Any
    current_task_source: str
    task_id: Any

    # Per-task isolated temp dir; None outside task pipeline.
    task_workspace: pathlib.Path | None
    attachments: list[dict[str, Any]]
    transport: dict[str, Any]

    # Boundary-checked path helpers.
    def repo_path(self, rel: str) -> pathlib.Path: ...
    def drive_path(self, rel: str) -> pathlib.Path: ...
    def drive_logs(self) -> pathlib.Path: ...


__all__ = ["ToolContextProtocol"]
