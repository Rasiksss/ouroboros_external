"""Append-only audit log for tool calls.

One JSONL file per (user, project): `<data_dir>/audit/<user>/<project>.jsonl`.
Each line is one tool invocation — flat JSON so ops can grep/jq it.

Schema:
    ts          — float, unix seconds
    user_id     — string (auth user) or "anonymous"
    project_id  — string or "default"
    session_id  — string or null
    tool        — tool name from REGISTRY
    args        — JSON-encoded args, truncated to ~500 chars
    result      — first ~500 chars of handler return value
    latency_ms  — float, handler runtime
    error       — null on success, exception class+msg on failure

Per УКИИ-АУА: every tool call by an autonomous agent must be traceable
to its (user, project, session) trigger and replayable for post-hoc
review. JSONL is the cheapest format that satisfies that.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from finengine.store import journals

log = logging.getLogger(__name__)

_MAX_ARGS = 500
_MAX_RESULT = 500


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + "...(truncated)"


def log_tool_call(
    *,
    user_id: str | int | None,
    project_id: str | None,
    session_id: str | None,
    tool_name: str,
    args: dict[str, Any],
    result_preview: str,
    latency_ms: float,
    error: str | None = None,
) -> None:
    """Append a tool-call event. Best-effort — never raises into caller."""
    uid = journals.safe_segment(str(user_id) if user_id is not None else "anonymous")
    pid = journals.safe_segment(project_id or "default")

    try:
        args_json = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as e:
        args_json = f"<unserializable args: {e}>"

    entry = {
        "ts": time.time(),
        "user_id": uid,
        "project_id": pid,
        "session_id": session_id,
        "tool": tool_name,
        "args": _truncate(args_json, _MAX_ARGS),
        "result": _truncate(result_preview, _MAX_RESULT),
        "latency_ms": round(latency_ms, 1),
        "error": error,
    }
    journals.append(journals.AUDIT, owner=uid, key=pid, entry=entry)
