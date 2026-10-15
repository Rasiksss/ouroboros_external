"""Append-only log of every extraction run.

Each completed run lands as one JSON object in
`<docs_dir>/extract_runs.jsonl`. The file is the source of truth for the
benchmark builder — `tests/build_unified_benchmark.py` (and a future
"per-arch / per-company-type" view) will read it to score recall/precision
against GT and accumulate statistics over time.

Why JSONL on disk instead of a DB:
- The runs are append-only and read in bulk for analysis — no random access
  is needed.
- Lives in the same docker volume as everything else, gets backed up by
  the daily backup.sh.
- Easy to inspect with `tail -f` and `jq`.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from finengine.config import settings


_LOCK = threading.Lock()
_LOG_PATH = settings.docs_dir / "extract_runs.jsonl"
_CHAT_LOG_PATH = settings.docs_dir / "chat_runs.jsonl"
# Per-chain-run event journal. Each completed chain analysis lands as a
# single jsonl record (header + all events) so we can replay it after
# client reconnect. `_CHAIN_EVENTS_DIR` holds the per-run streaming
# log used by the resume endpoint.
_CHAIN_LOG_PATH = settings.docs_dir / "chain_runs.jsonl"
_CHAIN_EVENTS_DIR = settings.docs_dir / "chain_events"
# Quick-synthesis runs (Finengine «Быстрый анализ» — one-shot off doc text).
# Persisted so the user can re-read their past commentaries even if their
# browser cache is wiped. Mirrors the chat/extract/chain audit trail.
_QUICK_LOG_PATH = settings.docs_dir / "quick_synthesis_runs.jsonl"
# Captured at import time so every run record carries the deploy version that
# produced it. `update.sh` on prod sets FINENGINE_GIT_VER (human-readable
# `vN` where N = commit count on main) + FINENGINE_GIT_SHA (immutable
# short-sha). Local dev gets "local-dev" for both.
_GIT_SHA = os.environ.get("FINENGINE_GIT_SHA", "").strip() or "local-dev"
_GIT_VER = os.environ.get("FINENGINE_GIT_VER", "").strip() or "local-dev"


def log_run(
    *,
    doc_id: str,
    filename: str,
    arch: str,
    model_id: str | None,
    company_type: str,
    company_name: str,
    period_current: str,
    period_prev: str,
    user_id: int | None,
    started_at: float,
    metrics: list[dict[str, Any]],
    error: str | None = None,
    diag: dict | None = None,
    # Phase A — extractor matrix axes. Optional during rollout: callers
    # that haven't been updated still log valid rows; the bench harness
    # treats missing values as "unknown" so historical runs keep working.
    profile_id: str = "",
    standard: str = "",
    language: str = "",
    profile_confidence_low: bool = False,
    # Reasoning effort ("low"/"medium"/"high") and extraction mode
    # ("registry"/"unrestricted" for finengine_dev). Logged so bench comparisons
    # of "same arch × different settings" can tell runs apart. "" = not captured.
    reasoning_effort: str = "",
    mode: str = "",
) -> None:
    """Append one record to the runs log. Best-effort: errors are swallowed
    (logging musn't break extract)."""
    try:
        elapsed = time.time() - started_at
        found = sum(1 for m in metrics if m.get("found"))
        total = len(metrics)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "git_ver": _GIT_VER,
            "git_sha": _GIT_SHA,
            "doc_id": doc_id,
            "filename": filename,
            "arch": arch,
            "model_id": model_id or "",
            "company_type": company_type,
            # New matrix axes — see project_backlog_extractor_matrix.md.
            "profile_id": profile_id,
            "standard": standard,
            "language": language,
            "profile_confidence_low": profile_confidence_low,
            "reasoning_effort": reasoning_effort,
            "mode": mode,
            "company_name": company_name,
            "period_current": period_current,
            "period_prev": period_prev,
            "user_id": user_id,
            "elapsed_s": round(elapsed, 1),
            "found": found,
            "total": total,
            "metrics": [
                {
                    "enum": m.get("enum"),
                    "value_current": m.get("value_current"),
                    "value_prev": m.get("value_prev"),
                    "period_values": m.get("period_values") or [],
                    "unit": m.get("unit"),
                    "source_form": m.get("source_form") or "",
                    "is_calculated": m.get("is_calculated", False),
                    "segment": m.get("segment") or "",
                    "dimension": m.get("dimension") or "",
                }
                for m in metrics
            ],
            "error": error,
            "diag": diag or {},
        }
        with _LOCK:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
    except Exception:
        # Never let logging break the request flow.
        pass


def runs_path() -> Path:
    return _LOG_PATH


def chat_runs_path() -> Path:
    return _CHAT_LOG_PATH


def log_chat_turn(
    *,
    session_id: str,
    user_id: int | None,
    username: str | None,
    doc_ids: list[str],
    message: str,
    response: str,
    started_at: float,
    arch: str = "",
    model_id: str | None = None,
    error: str | None = None,
) -> None:
    """Append one chat turn (user prompt + full assistant response) to
    `chat_runs.jsonl`. Used so we can audit what every user has asked the
    agent and what it answered — invaluable when testing with multiple
    users hits the wild and someone reports a wrong answer.

    Best-effort: errors are swallowed so logging never breaks the chat
    stream (same contract as `log_run`).
    """
    try:
        elapsed = time.time() - started_at
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "git_ver": _GIT_VER,
            "git_sha": _GIT_SHA,
            "session_id": session_id,
            "user_id": user_id,
            "username": username or "",
            "doc_ids": doc_ids,
            "arch": arch,
            "model_id": model_id or "",
            "message": message,
            "response": response,
            "elapsed_s": round(elapsed, 1),
            "error": error,
        }
        with _LOCK:
            _CHAT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_CHAT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
    except Exception:
        pass


def quick_synthesis_runs_path() -> Path:
    return _QUICK_LOG_PATH


def log_quick_synthesis(
    *,
    doc_id: str,
    doc_filename: str,
    company_name: str,
    company_type: str,
    arch: str,
    model_id: str | None,
    user_id: int | None,
    username: str | None,
    started_at: float,
    response: str,
    error: str | None = None,
) -> None:
    """Append one record per quick-synthesis run. Used so the user's
    browser-side dialog history can be reconstructed after a cache wipe
    or device switch — quick_synthesis text is otherwise streamed and
    never stored. Best-effort: errors are swallowed."""
    try:
        elapsed = time.time() - started_at
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "git_ver": _GIT_VER,
            "git_sha": _GIT_SHA,
            "doc_id": doc_id,
            "doc_filename": doc_filename,
            "company_name": company_name,
            "company_type": company_type,
            "arch": arch,
            "model_id": model_id or "",
            "user_id": user_id,
            "username": username or "",
            "elapsed_s": round(elapsed, 1),
            "response_chars": len(response or ""),
            "response": response,
            "error": error,
        }
        with _LOCK:
            _QUICK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_QUICK_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
    except Exception:
        pass


def chain_runs_path() -> Path:
    return _CHAIN_LOG_PATH


def chain_events_dir() -> Path:
    return _CHAIN_EVENTS_DIR


def chain_events_file(run_id: str) -> Path:
    """Per-run event-stream file. One JSON event per line; tailed by the
    resume endpoint when a client reconnects after page reload / VPN
    drop."""
    return _CHAIN_EVENTS_DIR / f"{run_id}.jsonl"


def append_chain_event(run_id: str, event: dict) -> None:
    """Append one SSE event to the per-run journal. The resume endpoint
    reads this from start, then tails for new events. Best-effort —
    failure to write must not break the chain analysis itself."""
    try:
        path = chain_events_file(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass


def log_chain_run(
    *,
    run_id: str,
    doc_id: str,
    user_id: int | None,
    username: str | None,
    arch: str,
    model_id: str | None,
    company_type: str,
    started_at: float,
    finished_at: float,
    chains_planned: int,
    chains_completed: int,
    error: str | None = None,
) -> None:
    """Append a summary record after a chain run completes (or fails).
    Detailed events are in chain_events/{run_id}.jsonl; this is the
    aggregate audit row."""
    try:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "git_ver": _GIT_VER,
            "git_sha": _GIT_SHA,
            "run_id": run_id,
            "doc_id": doc_id,
            "user_id": user_id,
            "username": username or "",
            "arch": arch,
            "model_id": model_id or "",
            "company_type": company_type,
            "elapsed_s": round(finished_at - started_at, 1),
            "chains_planned": chains_planned,
            "chains_completed": chains_completed,
            "error": error,
        }
        with _LOCK:
            _CHAIN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_CHAIN_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
    except Exception:
        pass
