"""Transport-specific NDJSON encoding for streamed exec events."""

from __future__ import annotations

import json

from app.services.runtime.types import ExecStreamEvent


class SessionStreamEncoder:
    def encode(self, event: ExecStreamEvent) -> bytes:
        payload: dict[str, object] = {"kind": event.kind}
        if event.data is not None:
            payload["data"] = event.data
        if event.exit_code is not None:
            payload["exitCode"] = event.exit_code
        if event.duration_ms is not None:
            payload["durationMs"] = event.duration_ms
        if event.timed_out is not None:
            payload["timedOut"] = event.timed_out
        if event.disconnected:
            payload["disconnected"] = True
        if event.workspace_root is not None:
            payload["workspaceRoot"] = event.workspace_root
        return (json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
