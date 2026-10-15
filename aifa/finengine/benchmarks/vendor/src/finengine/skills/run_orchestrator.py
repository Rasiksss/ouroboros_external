"""Generic resumable run orchestration.

Generalised from the chain-only `chain_orchestrator` so that every
streaming endpoint (extract / chat / quick_synthesis / analyze_chains / insights)
shares one background-task + journal + resume pattern.

How it works:

1. `start_run(runner, kind=…, meta=…)` spawns the runner as an
   `asyncio.create_task` *independent of the request that started it*.
   Client disconnect kills the request, not the task.
2. Each event the runner emits is:
   - appended to `data/documents/{kind}_events/{run_id}.jsonl`
   - dispatched to all live `asyncio.Queue` observers
3. `subscribe(run_id, kind=…)` returns an async generator that yields
   every event already in the journal (catch-up) then tails new events
   from a fresh queue until the run finishes — works whether you
   connect at start, mid-run or after completion.
4. On backend startup, `recover_orphan_runs()` scans all known kinds
   and appends a synthetic `session_lost` + `done` to any journal
   that died mid-stream so the next subscriber can finalise instead
   of spinning.

Storage layout — one directory per kind, sibling to docs_dir:
  data/documents/chain_events/{run_id}.jsonl
  data/documents/extract_events/{run_id}.jsonl
  data/documents/quick_events/{run_id}.jsonl
  data/documents/chat_events/{run_id}.jsonl
  data/documents/insights_events/{run_id}.jsonl

The kind directory is created on first write. Legacy chain_events stays
where it was so existing journals are unaffected.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Iterable

from finengine.config import settings


# Known run kinds. Each maps to its events directory under docs_dir.
# Extending: add a new kind here, and any code that wants to enumerate
# all run journals (orphan recovery) picks it up automatically.
KNOWN_KINDS: tuple[str, ...] = (
    "chain",
    "extract",
    "quick",
    "chat",
    "synth",
    "insights",
)


def _events_dir(kind: str) -> Path:
    """Directory holding journal files for the given kind."""
    return settings.docs_dir / f"{kind}_events"


def event_file(run_id: str, kind: str) -> Path:
    """Per-run journal path. The resume endpoint replays this from disk
    when the run isn't (or no longer is) in the in-memory registry."""
    return _events_dir(kind) / f"{run_id}.jsonl"


_FILE_LOCK = threading.Lock()


def append_event(run_id: str, kind: str, event: dict) -> None:
    """Append one SSE event to the per-run journal. Best-effort — failure
    to write must not break the run itself."""
    try:
        path = event_file(run_id, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _FILE_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
    except Exception:
        pass


# ── Per-run state ─────────────────────────────────────────────────────────
class RunState:
    """Mutable state for a single run, regardless of kind."""

    def __init__(self, run_id: str, kind: str, meta: dict):
        self.run_id = run_id
        self.kind = kind
        self.meta = meta
        self.events: list[dict] = []
        self.observers: list[asyncio.Queue[dict | None]] = []
        self.finished: bool = False
        self.started_at: float = time.time()
        self.finished_at: float | None = None
        self.error: str | None = None

    def emit(self, event: dict) -> None:
        """Append to journal + memory + broadcast to observers."""
        self.events.append(event)
        append_event(self.run_id, self.kind, event)
        for q in list(self.observers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def close(self, error: str | None = None) -> None:
        """Mark finished. All observers unblock with sentinel `None`."""
        self.finished = True
        self.error = error
        self.finished_at = time.time()
        for q in list(self.observers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass


# ── Registry ─────────────────────────────────────────────────────────────
_runs: dict[str, RunState] = {}


def get_run(run_id: str) -> RunState | None:
    return _runs.get(run_id)


def list_active(kind: str | None = None) -> list[str]:
    return [
        rid
        for rid, s in _runs.items()
        if not s.finished and (kind is None or s.kind == kind)
    ]


# ── Main entry point ─────────────────────────────────────────────────────
def start_run(
    runner: Callable[[], AsyncGenerator[dict[str, Any], None]],
    *,
    kind: str,
    meta: dict,
    on_done: Callable[[RunState], Awaitable[None]] | None = None,
) -> str:
    """Spawn a run as a background task and return its `run_id`.

    `runner` is a zero-arg callable that returns an async generator of
    events. Construction is deferred so the caller can capture state in
    a closure without starting iteration eagerly.

    `kind` selects the journal directory. Use one of `KNOWN_KINDS` (or
    add to that tuple if introducing a new run type).

    `meta` is opaque metadata stored alongside the run for the audit log
    (doc_id, arch, user_id, …). NOT exposed to subscribers.

    `on_done` is awaited once after the runner finishes (success or
    error). Use it to write the aggregate row to the appropriate
    *_runs.jsonl audit file.

    Caller is responsible for kicking off the SSE stream that subscribes
    to this run — `start_run` returns immediately; the task runs
    concurrently. Client disconnect on the kicker stream cancels the
    request task, not this one.
    """
    if kind not in KNOWN_KINDS:
        raise ValueError(f"unknown run kind: {kind!r}; expected one of {KNOWN_KINDS}")
    run_id = str(uuid.uuid4())
    state = RunState(run_id, kind, meta)
    _runs[run_id] = state

    async def _drive() -> None:
        # Set trace context before running. All LLM calls inside the
        # runner inherit run_id/kind/doc_id/user_id/arch via contextvars,
        # which means every call will land in llm_traces with the right
        # tags. Pulled from `meta` passed by the caller.
        from finengine.llm.tracer import set_trace_context, clear_trace_context
        set_trace_context(
            run_id=run_id,
            kind=kind,
            doc_id=meta.get("doc_id", ""),
            user_id=meta.get("user_id"),
            arch=meta.get("arch", ""),
        )
        try:
            async for ev in runner():
                state.emit(ev)
        except asyncio.CancelledError:
            state.emit({"type": "error", "message": "run cancelled"})
            state.close(error="cancelled")
            raise
        except Exception as e:
            state.emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
            state.close(error=f"{type(e).__name__}: {e}")
        else:
            state.close()
        finally:
            clear_trace_context()
            if on_done is not None:
                try:
                    await on_done(state)
                except Exception:
                    pass

    asyncio.create_task(_drive(), name=f"{kind}-run-{run_id[:8]}")
    return run_id


# ── Subscribe (initial stream + reattach after reload) ───────────────────
async def subscribe(run_id: str, *, kind: str | None = None) -> AsyncGenerator[dict, None]:
    """Yield every event for `run_id` — past + live.

    Resolution:
    - Live in-memory: replay everything emitted so far, then attach a
      queue and tail until the run closes.
    - Not in memory but journal exists on disk: replay the file and
      exit. The frontend treats the last event accordingly (e.g.
      `session_lost` → finalise with a notice).
    - Neither: yield a single `error` event and exit.

    `kind` lets callers narrow the journal lookup when the registry is
    cold (after a restart). If omitted we probe every known kind.
    """
    state = _runs.get(run_id)

    if state is not None:
        for ev in list(state.events):
            yield ev
        if state.finished:
            return
        q: asyncio.Queue[dict | None] = asyncio.Queue()
        state.observers.append(q)
        try:
            while True:
                ev = await q.get()
                if ev is None:
                    return
                yield ev
        finally:
            try:
                state.observers.remove(q)
            except ValueError:
                pass
        return

    # Not in memory → walk the journal.
    candidates: Iterable[str] = (kind,) if kind else KNOWN_KINDS
    for k in candidates:
        if k is None:
            continue
        path = event_file(run_id, k)
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
            return
        except Exception as e:
            yield {"type": "error", "message": f"journal read failed: {e}"}
            return

    yield {
        "type": "error",
        "message": (
            "Запуск не найден. Возможно, он завершился давно и журнал "
            "был удалён, либо идентификатор некорректен."
        ),
    }


# ── House-keeping ─────────────────────────────────────────────────────────
def cleanup_finished(max_age_seconds: float = 24 * 3600) -> int:
    """Drop finished runs older than `max_age_seconds` from the in-memory
    registry. Journal files on disk stay — they're append-only audit
    and cheap. Returns count of evicted runs."""
    now = time.time()
    evicted = 0
    for rid in list(_runs.keys()):
        s = _runs[rid]
        if s.finished and s.finished_at and (now - s.finished_at) > max_age_seconds:
            del _runs[rid]
            evicted += 1
    return evicted


# ── Orphan-run recovery on backend startup ───────────────────────────────
def recover_orphan_runs() -> dict[str, int]:
    """Scan every kind's event directory and finalise journals that died
    mid-stream — append synthetic `session_lost` + `done`. Idempotent:
    files already terminating in `done` / `session_lost` are skipped.

    Returns a per-kind dict of newly-marked counts.
    """
    result: dict[str, int] = {}
    for kind in KNOWN_KINDS:
        events_dir = _events_dir(kind)
        # Legacy: chain still keeps old chain_events directory; check both.
        legacy_dir = settings.docs_dir / "chain_events" if kind == "chain" else None

        marked = 0
        for d in (legacy_dir, events_dir):
            if d is None or not d.exists():
                continue
            for path in sorted(d.glob("*.jsonl")):
                try:
                    last_event_type = None
                    with open(path, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                ev = json.loads(line)
                                last_event_type = ev.get("type")
                            except json.JSONDecodeError:
                                continue
                    if last_event_type in {"done", "session_lost"}:
                        continue
                    run_id = path.stem
                    # Append directly to the file we just inspected — don't
                    # reroute legacy chain runs into the new directory.
                    with _FILE_LOCK:
                        with open(path, "a", encoding="utf-8") as f:
                            f.write(json.dumps({
                                "type": "session_lost",
                                "message": (
                                    "Прогон оборвался при перезапуске сервера. "
                                    "Запустите его заново — старые шаги остались в журнале."
                                ),
                            }, ensure_ascii=False) + "\n")
                            f.write(json.dumps({"type": "done"}, ensure_ascii=False) + "\n")
                            f.flush()
                            os.fsync(f.fileno())
                    marked += 1
                except Exception:
                    continue
        if marked:
            result[kind] = marked
    return result


__all__ = [
    "KNOWN_KINDS",
    "RunState",
    "append_event",
    "cleanup_finished",
    "event_file",
    "get_run",
    "list_active",
    "recover_orphan_runs",
    "start_run",
    "subscribe",
]
