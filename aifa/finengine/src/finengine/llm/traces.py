"""Per-(user, project) LLM trace log.

One JSONL line per LLM call — captures token usage and USD cost so we
can grep/jq spend by project, session, model, or call kind. Lives next
to the audit log but is separate because:
  - audit/ logs tool calls (one line per tool invocation)
  - llm_traces/ logs LLM calls (one line per LLM round-trip)

Two ways to write a trace:
  1. `log_turn(...)` — explicit call from the chat-stream path.
  2. ContextVar `trace_ctx` + automatic write inside
     `openrouter.complete_with_usage` — catches extraction / chains /
     synthesis paths that bypass the chat-stream code.

Schema (JSON per line):
    ts          float, unix seconds
    user_id     "anonymous" or sanitized id
    project_id  sanitized or "default"
    session_id  agent session id (may be empty)
    kind        "chat" | "extract" | "chains" | "synthesis" | "other"
    model       canonical name reported by gateway
    turns       int — for chat, how many LLM round-trips; else 1
    tokens_in   int (includes cached)
    tokens_out  int
    tokens_cached int
    cost_usd    float (rounded to 6 places)
"""
from __future__ import annotations

import contextvars
import logging
import time
from typing import Any

from finengine.llm.pricing import cost_usd
from finengine.store import journals

log = logging.getLogger(__name__)


# ContextVar set by request-entry handlers (chat / extract / chains).
# Read inside `openrouter.complete_with_usage` so every LLM call is
# attributed to its caller without threading user_id through every
# function. Reset is the caller's responsibility (use `set()` →
# `reset(token)` or set a fresh task-local).
trace_ctx: contextvars.ContextVar["TraceCtx | None"] = contextvars.ContextVar(
    "fa_claw_trace_ctx", default=None,
)


class TraceCtx:
    """Snapshot of who triggered the LLM call. Cheap dataclass-ish."""
    __slots__ = ("user_id", "project_id", "session_id", "kind")

    def __init__(
        self,
        user_id: str | int | None,
        project_id: str | None,
        session_id: str | None = None,
        kind: str = "other",
    ):
        self.user_id = user_id
        self.project_id = project_id
        self.session_id = session_id
        self.kind = kind


def log_call(usage: dict[str, Any], *, ctx: TraceCtx | None = None) -> None:
    """Write a single LLM-call trace. Uses provided `ctx` or falls back
    to the current `trace_ctx`. No-op if no ctx and no fallback.
    Safe to call from any code path — best-effort, never raises."""
    actual = ctx or trace_ctx.get()
    if actual is None:
        return
    _write_entry(
        user_id=actual.user_id,
        project_id=actual.project_id,
        session_id=actual.session_id,
        kind=actual.kind,
        usage=usage,
        turns=1,
    )


def _write_entry(
    *, user_id, project_id, session_id, kind, usage, turns,
) -> dict[str, Any]:
    uid = journals.safe_segment(str(user_id) if user_id is not None else "anonymous")
    pid = journals.safe_segment(project_id or "default")
    entry = {
        "ts": time.time(),
        "user_id": uid,
        "project_id": pid,
        "session_id": session_id or "",
        "kind": kind,
        "model": usage.get("model", "") or "",
        "turns": int(turns),
        "tokens_in": int(usage.get("in", 0) or 0),
        "tokens_out": int(usage.get("out", 0) or 0),
        "tokens_cached": int(usage.get("cached", 0) or 0),
        "cost_usd": cost_usd(usage),
    }
    journals.append(journals.LLM_TRACES, owner=uid, key=pid, entry=entry)
    return entry


def log_turn(
    *,
    user_id: str | int | None,
    project_id: str | None,
    session_id: str | None,
    usage: dict[str, Any],
) -> dict[str, Any]:
    """Append a chat-turn trace line and return the entry (so callers
    can echo cost back into the SSE stream for UI display). Aggregates
    multiple LLM round-trips inside one user message — see
    `usage.turns` from openclaw.stream_events."""
    return _write_entry(
        user_id=user_id,
        project_id=project_id,
        session_id=session_id,
        kind="chat",
        usage=usage,
        turns=int(usage.get("turns", 1) or 1),
    )


def project_totals(user_id: str | int | None, project_id: str | None) -> dict[str, Any]:
    """Sum tokens + cost across the project's trace file. Used by the
    UI to show 'spent in this project: $X.XX' without a DB query.
    Also broken down by kind (chat/extract/chains/synthesis/other)."""
    total = {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0,
             "cost_usd": 0.0, "turns_total": 0, "user_turns": 0,
             "by_kind": {}}
    for e in journals.read(journals.LLM_TRACES, owner=user_id, key=project_id or "default"):
        total["tokens_in"] += int(e.get("tokens_in", 0) or 0)
        total["tokens_out"] += int(e.get("tokens_out", 0) or 0)
        total["tokens_cached"] += int(e.get("tokens_cached", 0) or 0)
        total["cost_usd"] += float(e.get("cost_usd", 0.0) or 0.0)
        total["turns_total"] += int(e.get("turns", 0) or 0)
        total["user_turns"] += 1
        kind = e.get("kind", "other") or "other"
        by_kind = total["by_kind"].setdefault(kind, {"calls": 0, "cost_usd": 0.0})
        by_kind["calls"] += 1
        by_kind["cost_usd"] += float(e.get("cost_usd", 0.0) or 0.0)
    total["cost_usd"] = round(total["cost_usd"], 6)
    for k in total["by_kind"]:
        total["by_kind"][k]["cost_usd"] = round(total["by_kind"][k]["cost_usd"], 6)
    return total
