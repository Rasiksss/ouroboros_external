"""Universal LLM trace logger.

Hooks into `openrouter.complete_with_usage` and `openrouter.stream` so EVERY
LLM call across the project — chat, extract (any arch), quick_synthesis,
chains, consensus — produces an audit-trail line on disk.

Storage layout:
  data/documents/llm_traces/{YYYY-MM-DD}.jsonl

Each line is one record:
  {
    "ts": ISO timestamp,
    "run_id": orchestrator run id (or "" if not in a run),
    "kind": "chat" | "extract" | "chains" | "quick" | "synth" | "consensus" | ...,
    "doc_id": "" or the document being processed,
    "user_id": int or null,
    "model": full provider/model id,
    "arch": "" or the active extraction arch,
    "system_prompt": <truncated to TRUNCATE_CHARS>,
    "prompt": <truncated to TRUNCATE_CHARS>,
    "response": <truncated to TRUNCATE_CHARS>,
    "tokens_in": int,
    "tokens_out": int,
    "tokens_cached": int,
    "latency_s": float,
    "error": null or "...",
    "extra": dict of caller-specific tags (metric_enum, round, etc.)
  }

Why we need this — prior to this module, LLM calls left only aggregate stats
in `extract_runs.jsonl` (totals per run). When the Sber Triumvirat run burned
$12 on DeepSeek and the user asked "show me the dialog", we couldn't —
nothing persisted the raw I/O. This trace closes that gap permanently.
"""
from __future__ import annotations

import contextvars as _cv
import datetime as _dt
import json as _json
import os as _os
import time as _time
from pathlib import Path
from typing import Any

from finengine.config import settings


# Truncate any single field at this many characters before writing. Keeps
# trace files reasonable on disk (a single Triumvirat call can have 100K-tok
# input). A run that processes 100 metrics × 3 models still lands at
# ~10-50 MB of trace, which is fine — date-partitioned + rotatable.
TRUNCATE_CHARS = 50_000


# Per-asyncio-task context. Set in API request handlers before LLM calls;
# inherited by spawned tasks via asyncio's normal contextvar inheritance.
_TRACE_RUN_ID: _cv.ContextVar[str] = _cv.ContextVar("finengine_trace_run_id", default="")
_TRACE_KIND: _cv.ContextVar[str] = _cv.ContextVar("finengine_trace_kind", default="")
_TRACE_DOC_ID: _cv.ContextVar[str] = _cv.ContextVar("finengine_trace_doc_id", default="")
_TRACE_USER_ID: _cv.ContextVar[int | None] = _cv.ContextVar("finengine_trace_user_id", default=None)
_TRACE_ARCH: _cv.ContextVar[str] = _cv.ContextVar("finengine_trace_arch", default="")
_TRACE_EXTRA: _cv.ContextVar[dict] = _cv.ContextVar("finengine_trace_extra", default={})


def set_trace_context(
    *,
    run_id: str = "",
    kind: str = "",
    doc_id: str = "",
    user_id: int | None = None,
    arch: str = "",
) -> None:
    """Set trace metadata for the current asyncio task. Call this once at
    the entry of an API request before any LLM call. Spawned tasks
    (`asyncio.create_task`, `asyncio.gather`) inherit the context."""
    _TRACE_RUN_ID.set(run_id)
    _TRACE_KIND.set(kind)
    _TRACE_DOC_ID.set(doc_id)
    _TRACE_USER_ID.set(user_id)
    _TRACE_ARCH.set(arch)


def update_trace_extra(**kwargs: Any) -> None:
    """Merge caller-specific tags into the per-task `extra` dict. Use this
    inside per-metric loops to tag traces with `metric_enum`, `round`, etc.
    Replaces (rather than mutates) the dict so contextvar inheritance
    semantics stay clean."""
    cur = dict(_TRACE_EXTRA.get() or {})
    cur.update(kwargs)
    _TRACE_EXTRA.set(cur)


def clear_trace_context() -> None:
    """Reset all trace contextvars to empty. Optional — context is task-local
    so it auto-clears when the task finishes."""
    _TRACE_RUN_ID.set("")
    _TRACE_KIND.set("")
    _TRACE_DOC_ID.set("")
    _TRACE_USER_ID.set(None)
    _TRACE_ARCH.set("")
    _TRACE_EXTRA.set({})


def _trace_dir() -> Path:
    """Resolve the trace directory under the configured docs dir. Created
    lazily on first write."""
    p = Path(settings.docs_dir) / "llm_traces"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _trace_file_for(ts: _dt.datetime) -> Path:
    """One file per UTC date. Easy to rotate / delete old traces."""
    return _trace_dir() / f"{ts.strftime('%Y-%m-%d')}.jsonl"


def _truncate(s: str | None) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= TRUNCATE_CHARS:
        return s
    return s[:TRUNCATE_CHARS] + f"\n…[truncated {len(s) - TRUNCATE_CHARS} chars]"


def write_trace(
    *,
    model: str,
    system_prompt: str | None,
    prompt: str,
    response: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    tokens_cached: int = 0,
    latency_s: float = 0.0,
    error: str | None = None,
) -> None:
    """Write one trace record. Called from the openrouter wrapper after
    every LLM call (success or failure). Cheap — single line append, no
    fsync. If the write fails the LLM call still returns to the caller —
    we never want logging to break a real request.

    Disable via env `FINENGINE_DISABLE_LLM_TRACE=1` if disk is tight.
    """
    # Per-user demo spend accounting — independent of trace logging (runs even
    # when tracing is disabled). Best-effort; never breaks the LLM call.
    _uid = _TRACE_USER_ID.get()
    if _uid:
        try:
            from finengine.auth.billing import record_spend

            record_spend(_uid, model, tokens_in, tokens_out, tokens_cached)
        except Exception:
            pass
    if _os.environ.get("FINENGINE_DISABLE_LLM_TRACE"):
        return
    try:
        ts = _dt.datetime.utcnow()
        rec = {
            "ts": ts.isoformat(timespec="seconds") + "Z",
            "run_id": _TRACE_RUN_ID.get(),
            "kind": _TRACE_KIND.get(),
            "doc_id": _TRACE_DOC_ID.get(),
            "user_id": _TRACE_USER_ID.get(),
            "arch": _TRACE_ARCH.get(),
            "model": model,
            "system_prompt": _truncate(system_prompt),
            "prompt": _truncate(prompt),
            "response": _truncate(response),
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            "tokens_cached": int(tokens_cached or 0),
            "latency_s": round(float(latency_s), 3),
            "error": error,
            "extra": dict(_TRACE_EXTRA.get() or {}),
        }
        path = _trace_file_for(ts)
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        # Never let logging break the request flow. Errors here would
        # cascade into every LLM call across the system.
        pass


def time_now() -> float:
    """Convenience — exposed so the wrapper can stamp latency without
    re-importing time."""
    return _time.time()
