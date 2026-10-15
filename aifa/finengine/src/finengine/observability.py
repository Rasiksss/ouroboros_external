"""Session timeline log — every observable agent event in one JSONL.

One file per (user, session): `<data_dir>/traces/<user>/<session>.jsonl`.
Each line is one event with ISO timestamp, kind, and full payload (NO
truncation — debugging needs the raw bytes).

Events captured:
  - session_start       — user_id, project_id, message, model
  - prompt_built        — full prompt text, length, sources
  - llm_turn_start      — turn idx, items count, payload size
  - llm_text_delta      — text chunk model emitted (per response.output_text.delta)
  - llm_tool_call       — function_call from model with full args
  - tool_executing      — backend dispatching to handler
  - tool_result         — handler output (full, including big JSON)
  - llm_turn_end        — turn idx, usage, completed flag
  - sse_emit            — every event we yield to the frontend
  - error               — error event with class + message
  - session_done        — total turns, total cost, duration

Designed to be append-only, no rotation. Files grow; ops can rotate
externally if needed. Best-effort writes — never raises into caller.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from finengine.store import journals

log = logging.getLogger(__name__)


def _iso_now() -> str:
    """ISO-8601 UTC with millisecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def emit(
    *,
    user_id: str | int | None,
    session_id: str | None,
    event: str,
    **payload: Any,
) -> None:
    """Append one timeline event. Falls back to user=anonymous /
    session=default when those are missing (e.g. early in the request
    before auth is resolved)."""
    uid = journals.safe_segment(str(user_id) if user_id is not None else "anonymous")
    sid = journals.safe_segment(session_id or "default")

    entry: dict[str, Any] = {
        "ts": time.time(),
        "ts_iso": _iso_now(),
        "event": event,
        "user_id": uid,
        "session_id": sid,
    }
    # Merge payload — caller-provided fields go after the metadata so
    # they can't accidentally override ts/event/user.
    for k, v in payload.items():
        if k in entry:
            entry[f"_{k}"] = v
        else:
            entry[k] = v

    journals.append(journals.TRACES, owner=uid, key=sid, entry=entry)


def read_session(user_id: str | int | None, session_id: str) -> list[dict]:
    """Read full timeline for a session (used by /admin/trace endpoint
    for debugging)."""
    return journals.read(journals.TRACES, owner=user_id, key=session_id)
