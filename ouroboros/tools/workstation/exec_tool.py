"""``code_exec`` — spec §19.3's ``exec``.

No read-only restriction: every other WorkStation tool refuses when the
workspace access level is not ``rw``; this one does not, since it can write
inside the session's ``/tmp`` or elsewhere outside the workspace subtree,
which the workspace access level does not govern.
"""

from __future__ import annotations

from typing import Any

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.egress.workstation import WorkstationError
from ouroboros.egress.workstation.executor import ExecOutcome
from ouroboros.egress.workstation.paths import WorkstationPathError, normalize_workspace_path
from ouroboros.tools.workstation._shared import render_error, unavailable_result

_SCHEMA = ToolSchema(
    name="code_exec",
    description=(
        "Execute a shell command inside the workstation sandbox session and return its exit code, stdout, and stderr."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "workdir": {"type": "string", "description": "Working directory (defaults to cwd)"},
            "env": {"type": "object"},
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds (optional, kills process on expiry)",
            },
        },
        "required": ["command"],
    },
)


def _render_outcome(outcome: ExecOutcome) -> str:
    parts: list[str] = []
    if outcome.stdout:
        parts.append(outcome.stdout)
        if outcome.stdout_overflowed:
            parts.append("⚠️ OMISSION NOTE: code_exec truncated stdout (output too large).")
    if outcome.stderr:
        parts.append(outcome.stderr)
        if outcome.stderr_overflowed:
            parts.append("⚠️ OMISSION NOTE: code_exec truncated stderr (output too large).")
    combined = "\n".join(parts) if parts else "(no output)"
    footer = f"exit_code={outcome.exit_code}"
    if outcome.timed_out:
        footer += " timed_out=true"
    return f"{combined}\n\n{footer}"


def _handle_code_exec(
    ctx: ToolContext,
    command: str = "",
    workdir: str = "",
    env: dict[str, Any] | None = None,
    timeout: Any = None,
    **_kwargs: Any,
) -> str:
    handle = ctx.workstation
    if handle is None:
        return unavailable_result("code_exec")
    if not str(command or "").strip():
        return "⚠️ CODE_EXEC_ERROR: 'command' is required."

    timeout_sec: int | None = None
    if timeout is not None:
        try:
            timeout_sec = int(timeout)
        except (TypeError, ValueError):
            return f"⚠️ CODE_EXEC_ERROR: 'timeout' must be a number, got {timeout!r}."

    cwd = handle.config.workspace_root
    if workdir:
        try:
            cwd = normalize_workspace_path(workdir, workspace_root=handle.config.workspace_root)
        except WorkstationPathError as exc:
            return f"⚠️ CODE_EXEC_ERROR: invalid workdir: {exc}"

    try:
        outcome = handle.commands.run_streamed(
            ["/bin/sh", "-c", command],
            workdir=cwd,
            env=env or None,
            timeout_sec=timeout_sec,
        )
    except WorkstationError as exc:
        return render_error("code_exec", exc)
    return _render_outcome(outcome)


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="code_exec",
            schema=_SCHEMA,
            handler=_handle_code_exec,
            is_code_tool=True,
            timeout_sec=360,
        ),
    ]


__all__ = ["get_tools"]
