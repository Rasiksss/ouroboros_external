"""Typed runtime contracts shared across runtime modes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class RuntimeContext:
    session_id: str
    rootfs_path: str


@dataclass(slots=True, frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    kill_reason: str | None = None


@dataclass(slots=True, frozen=True)
class ExecStreamEvent:
    kind: str
    data: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    timed_out: bool | None = None
    disconnected: bool | None = None
    workspace_root: str | None = None
    kill_reason: str | None = None
