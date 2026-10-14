"""Command execution interface for AEF WorkStation (spec §19.2b).

Not a tool: the model-facing ``code_exec`` tool and the file bridge's
mkdir/rm/rename helpers are the only callers. Bound to one session's client
and token; policy-free — exit-code interpretation belongs to the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ouroboros.egress.workstation.client import WorkstationClient, WorkstationConfig

#: Local caps independent of whatever the service itself truncates at
#: (spec §12.3 says service-side truncation is normal) — the ultimate
#: consumer here is LLM context, so the same defensive cap and wording as
#: ``ouroboros/tools/skill_exec.py:143-148`` applies.
_MAX_STDOUT_BYTES = 256 * 1024
_MAX_STDERR_BYTES = 128 * 1024


@dataclass(frozen=True)
class ExecOutcome:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    stdout_overflowed: bool
    stderr_overflowed: bool


def _cap_text(text: str, limit_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text, False
    return encoded[:limit_bytes].decode("utf-8", errors="ignore"), True


class WorkstationCommandExecutor:
    def __init__(self, client: WorkstationClient, *, session_id: str, token: str, config: WorkstationConfig) -> None:
        self._client = client
        self._session_id = session_id
        self._token = token
        self._config = config

    def run_buffered(
        self,
        command: list[str],
        *,
        workdir: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> ExecOutcome:
        """Short helper commands with no model-visible output (spec §12.1) —
        used internally by the file bridge for mkdir/rm/rename."""
        result = self._client.exec_buffered(
            session_id=self._session_id,
            token=self._token,
            command=command,
            cwd=workdir or self._config.workspace_root,
            env=env,
            stdin=None,
            timeout_sec=timeout_sec or self._config.command_timeout_sec,
        )
        stdout, stdout_overflowed = _cap_text(result.stdout, _MAX_STDOUT_BYTES)
        stderr, stderr_overflowed = _cap_text(result.stderr, _MAX_STDERR_BYTES)
        return ExecOutcome(
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=result.timed_out,
            stdout_overflowed=stdout_overflowed,
            stderr_overflowed=stderr_overflowed,
        )

    def run_streamed(
        self,
        command: list[str],
        *,
        workdir: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> ExecOutcome:
        """The model-facing ``code_exec`` path (spec §12.2) — live NDJSON
        consumption for correct timeout/incremental behavior, buffered here
        into one result since the tool layer has no partial-output channel."""
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        terminal: dict[str, Any] = {}

        def _on_event(event: dict[str, Any]) -> None:
            kind = event.get("kind")
            if kind == "stdout":
                stdout_chunks.append(str(event.get("data") or ""))
            elif kind == "stderr":
                stderr_chunks.append(str(event.get("data") or ""))
            elif kind == "exit":
                terminal.update(event)

        self._client.exec_stream(
            session_id=self._session_id,
            token=self._token,
            command=command,
            cwd=workdir or self._config.workspace_root,
            env=env,
            stdin=None,
            timeout_sec=timeout_sec or self._config.command_timeout_sec,
            on_event=_on_event,
        )
        stdout, stdout_overflowed = _cap_text("".join(stdout_chunks), _MAX_STDOUT_BYTES)
        stderr, stderr_overflowed = _cap_text("".join(stderr_chunks), _MAX_STDERR_BYTES)
        return ExecOutcome(
            exit_code=int(terminal.get("exitCode") or 0),
            stdout=stdout,
            stderr=stderr,
            timed_out=bool(terminal.get("timedOut")),
            stdout_overflowed=stdout_overflowed,
            stderr_overflowed=stderr_overflowed,
        )


__all__ = ["ExecOutcome", "WorkstationCommandExecutor"]
