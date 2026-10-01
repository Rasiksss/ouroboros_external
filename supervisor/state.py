"""Supervisor persistent state and LLM usage accounting (owner state in runtime DB)."""

from __future__ import annotations

import contextlib
import logging
import pathlib
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

from ouroboros.storage.blob.keys import storage_tenant_id
from ouroboros.storage.owner_state import default_state_dict, ensure_state_defaults
from ouroboros.storage.runtime_backend import RuntimeStore
from ouroboros.storage.runtime_models import OwnerRuntimeState, RuntimeSession, TaskSource

__all__ = [
    "OwnerRuntimeState",
    "RuntimeSession",
    "RuntimeStateRepository",
    "TaskSource",
    "append_jsonl",
    "get_state_repository",
    "init",
    "init_state",
    "init_state_for_repo",
    "load_state",
    "model_breakdown",
    "owner_state_transaction",
    "per_task_usage_summary",
    "rotate_chat_log_if_needed",
    "save_state",
    "status_text",
    "update_usage_from_llm",
]
from ouroboros.storage.runtime_store import get_runtime_store, init_runtime_store

log = logging.getLogger(__name__)

# Owner state is one row read and written whole, so every read-modify-write of it has to
# exclude the others or the last writer silently reverts fields it never touched. The lock
# is module-level because `get_state_repository(drive_root)` hands out a fresh repository
# on every call.
_state_lock = threading.RLock()


@contextlib.contextmanager
def owner_state_transaction() -> Iterator[None]:
    """Hold the owner-state lock around a load/modify/save that cannot use ``mutate``."""
    with _state_lock:
        yield


def append_jsonl(path: pathlib.Path, payload: dict[str, Any]) -> bool:
    from ouroboros.utils import append_jsonl as _append_jsonl

    return _append_jsonl(pathlib.Path(path), payload)


class RuntimeStateRepository:
    """DB-backed owner runtime state with session sync into runtime metadata store."""

    def __init__(
        self,
        drive_root: pathlib.Path,
        *,
        store: RuntimeStore | None = None,
    ) -> None:
        self.drive_root = pathlib.Path(drive_root)
        self._store = store or get_runtime_store(self.drive_root) or init_runtime_store(self.drive_root)
        self._tenant_id = storage_tenant_id()

    def load(self) -> OwnerRuntimeState:
        raw = self._store.load_owner_runtime_state(self._tenant_id)
        if raw is None:
            st = default_state_dict()
            self._store.save_owner_runtime_state(st, self._tenant_id)
            return OwnerRuntimeState.model_validate(st)
        normalized = ensure_state_defaults(dict(raw))
        if set(raw) - set(normalized):
            self._store.save_owner_runtime_state(normalized, self._tenant_id)
        state = OwnerRuntimeState.model_validate(normalized)
        self._refresh_usage_from_store(state)
        return state

    def session_id(self) -> str:
        """Just the session id, without ``load``'s usage refresh.

        That refresh is a COUNT/SUM over the session's whole ``llm_usage`` table under the
        store lock, and a writer that only needs to stamp a row with its session pays it for
        nothing — on every chat message there is.
        """
        raw = self._store.load_owner_runtime_state(self._tenant_id)
        if raw is None:
            return str(self.load().session_id or "")
        return str(raw.get("session_id") or "")

    def save(self, state: OwnerRuntimeState) -> None:
        payload = ensure_state_defaults(state.model_dump(mode="json", exclude_none=False))
        self._store.save_owner_runtime_state(payload, self._tenant_id)

    @contextlib.contextmanager
    def mutate(self) -> Iterator[OwnerRuntimeState]:
        """Read owner state, let the caller edit it, and write it back as one step."""
        with _state_lock:
            state = self.load()
            yield state
            self.save(state)

    def sync_runtime_session(self, store: Any, *, source: TaskSource = "unknown") -> None:
        state = self.load()
        if not state.session_id:
            state.session_id = uuid.uuid4().hex
            self.save(state)
        owner_ref = str(state.owner_id or state.owner_chat_id or "")
        store.create_or_update_session(
            RuntimeSession(
                session_id=state.session_id,
                source=source,
                owner_external_id=owner_ref,
                status="active",
            )
        )

    def _refresh_usage_from_store(self, state: OwnerRuntimeState) -> None:
        try:
            stats = self._store.sum_usage_stats(session_id=str(state.session_id or "") or None)
            state.spent_calls = int(stats.get("spent_calls") or 0)
            state.spent_tokens_prompt = int(stats.get("spent_tokens_prompt") or 0)
            state.spent_tokens_completion = int(stats.get("spent_tokens_completion") or 0)
            state.spent_tokens_cached = int(stats.get("spent_tokens_cached") or 0)
        except Exception:
            log.debug("Failed to refresh usage counters from llm_usage", exc_info=True)


def init(drive_root: pathlib.Path) -> RuntimeStateRepository:
    """Create a state repository for tests; production uses build_supervisor_runtime."""
    store = init_runtime_store(pathlib.Path(drive_root))
    return RuntimeStateRepository(pathlib.Path(drive_root), store=store)


def get_state_repository(drive_root: pathlib.Path | None = None) -> RuntimeStateRepository:
    if drive_root is not None:
        store = get_runtime_store(drive_root) or init_runtime_store(drive_root)
        return RuntimeStateRepository(pathlib.Path(drive_root), store=store)
    from supervisor.runtime_context import try_get_runtime

    runtime = try_get_runtime()
    if runtime is not None:
        return runtime.state_repo
    raise RuntimeError("RuntimeStateRepository is not available; pass drive_root or initialize supervisor runtime")


def load_state() -> OwnerRuntimeState:
    return get_state_repository().load()


def current_session_id() -> str:
    return get_state_repository().session_id()


def save_state(state: OwnerRuntimeState) -> None:
    get_state_repository().save(state)


def init_state_for_repo(repo: RuntimeStateRepository) -> OwnerRuntimeState:
    """Ensure owner state row exists with a session id."""
    state = repo.load()
    if not state.session_id:
        state.session_id = uuid.uuid4().hex
        repo.save(state)
    return state


def init_state() -> OwnerRuntimeState:
    return init_state_for_repo(get_state_repository())


def update_usage_from_llm(usage: dict[str, Any]) -> None:
    """Refresh LLM call/token counters from durable usage stats."""

    def _to_int(v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except Exception:
            log.debug("Failed to convert value to int: %r", v, exc_info=True)
            return default

    repo = get_state_repository()
    rounds = _to_int(usage.get("rounds") if isinstance(usage, dict) else 0, default=1)
    with repo.mutate() as state:
        try:
            store = get_runtime_store(repo.drive_root) or init_runtime_store(repo.drive_root)
            stats = store.sum_usage_stats(session_id=str(state.session_id or "") or None)
            state.spent_calls = int(stats.get("spent_calls") or 0)
            state.spent_tokens_prompt = _to_int(stats.get("spent_tokens_prompt") or 0)
            state.spent_tokens_completion = _to_int(stats.get("spent_tokens_completion") or 0)
            state.spent_tokens_cached = _to_int(stats.get("spent_tokens_cached") or 0)
        except Exception:
            state.spent_calls = int(state.spent_calls or 0) + rounds
            state.spent_tokens_prompt = _to_int(state.spent_tokens_prompt or 0) + _to_int(
                usage.get("prompt_tokens") if isinstance(usage, dict) else 0
            )
            state.spent_tokens_completion = _to_int(state.spent_tokens_completion or 0) + _to_int(
                usage.get("completion_tokens") if isinstance(usage, dict) else 0
            )
            state.spent_tokens_cached = _to_int(state.spent_tokens_cached or 0) + _to_int(
                usage.get("cached_tokens") if isinstance(usage, dict) else 0
            )


def model_breakdown() -> dict[str, dict[str, float]]:
    """Aggregate llm_usage calls/tokens by model from runtime events."""
    breakdown: dict[str, dict[str, float]] = {}
    try:
        from ouroboros.observability.event_record import list_runtime_events

        events = list_runtime_events(get_state_repository().drive_root, event_type="llm_usage", limit=1_000_000)
        for event in events:
            try:
                model = event.get("model") or "unknown"
                prompt_tokens = int(event.get("prompt_tokens", 0) or 0)
                completion_tokens = int(event.get("completion_tokens", 0) or 0)
                cached_tokens = int(event.get("cached_tokens", 0) or 0)
            except (ValueError, TypeError, AttributeError):
                continue

            if model not in breakdown:
                breakdown[model] = {
                    "calls": 0.0,
                    "prompt_tokens": 0.0,
                    "completion_tokens": 0.0,
                    "cached_tokens": 0.0,
                }

            breakdown[model]["calls"] += 1
            breakdown[model]["prompt_tokens"] += prompt_tokens
            breakdown[model]["completion_tokens"] += completion_tokens
            breakdown[model]["cached_tokens"] += cached_tokens
    except Exception:
        log.warning("Failed to calculate model breakdown", exc_info=True)

    return breakdown


def per_task_usage_summary(max_tasks: int = 10, tail_bytes: int = 512_000) -> list[dict[str, Any]]:
    """Return recent per-task token usage from runtime events."""
    del tail_bytes
    repo = get_state_repository()

    tasks: dict[str, dict[str, Any]] = {}
    try:
        from ouroboros.observability.event_record import list_runtime_events

        for event in list_runtime_events(repo.drive_root, event_type="llm_usage", limit=5000):
            try:
                tid = event.get("task_id") or "unknown"
                prompt_tokens = int(event.get("prompt_tokens", 0) or 0)
                completion_tokens = int(event.get("completion_tokens", 0) or 0)
            except (ValueError, TypeError, AttributeError):
                continue
            if tid not in tasks:
                tasks[tid] = {
                    "task_id": tid,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "rounds": 0,
                    "model": event.get("model", ""),
                }
            tasks[tid]["prompt_tokens"] += prompt_tokens
            tasks[tid]["completion_tokens"] += completion_tokens
            tasks[tid]["rounds"] += 1
    except Exception:
        log.warning("Failed to calculate per-task usage summary", exc_info=True)

    sorted_tasks = sorted(
        tasks.values(),
        key=lambda x: int(x.get("prompt_tokens", 0)) + int(x.get("completion_tokens", 0)),
        reverse=True,
    )
    return sorted_tasks[:max_tasks]


def status_text(workers_dict: dict[int, Any] | Any, soft_timeout_sec: int, hard_timeout_sec: int) -> str:
    """Build status text from worker and queue state."""
    from supervisor.queue import list_pending_tasks, list_running_tasks

    pending_list = list_pending_tasks()
    running_dict = list_running_tasks()
    state = load_state()
    now = time.time()
    lines = []
    lines.append(f"owner_id: {state.owner_id}")
    lines.append(f"session_id: {state.session_id}")
    worker_values = workers_dict.all_slots() if hasattr(workers_dict, "all_slots") else workers_dict.values()
    worker_list = list(worker_values)
    busy_count = sum(1 for w in worker_list if getattr(w, "busy_task_id", None) is not None)
    lines.append(f"workers: {len(worker_list)} (busy: {busy_count})")
    lines.append(f"pending: {len(pending_list)}")
    lines.append(f"running: {len(running_dict)}")
    if pending_list:
        preview = []
        for t in pending_list[:10]:
            preview.append(f"{t.get('id')}:{t.get('type')}:pr{t.get('priority')}:a{int(t.get('_attempt') or 1)}")
        lines.append("pending_queue: " + ", ".join(preview))
    if running_dict:
        lines.append("running_ids: " + ", ".join(list(running_dict.keys())[:10]))
    busy = [
        f"{getattr(w, 'wid', '?')}:{getattr(w, 'busy_task_id', '?')}"
        for w in worker_list
        if getattr(w, "busy_task_id", None)
    ]
    if busy:
        lines.append("busy: " + ", ".join(busy))
    if running_dict:
        details = []
        for task_id, meta in list(running_dict.items())[:10]:
            _task_raw = meta.get("task") if isinstance(meta, dict) else None
            task = _task_raw if isinstance(_task_raw, dict) else {}
            started = float(meta.get("started_at") or 0.0) if isinstance(meta, dict) else 0.0
            hb = float(meta.get("last_heartbeat_at") or 0.0) if isinstance(meta, dict) else 0.0
            runtime_sec = int(max(0.0, now - started)) if started > 0 else 0
            hb_lag_sec = int(max(0.0, now - hb)) if hb > 0 else -1
            details.append(
                f"{task_id}:type={task.get('type')} pr={task.get('priority')} "
                f"attempt={meta.get('attempt')} runtime={runtime_sec}s hb_lag={hb_lag_sec}s"
            )
        if details:
            lines.append("running_details:")
            lines.extend([f"  - {d}" for d in details])
    if running_dict and busy_count == 0:
        lines.append("queue_warning: running>0 while busy=0")
    lines.append(f"spent_calls: {state.spent_calls}")
    lines.append(
        f"prompt_tokens: {state.spent_tokens_prompt}, completion_tokens: {state.spent_tokens_completion}, "
        f"cached_tokens: {state.spent_tokens_cached}"
    )

    models = model_breakdown()
    if models:
        sorted_models = sorted(models.items(), key=lambda x: x[1]["calls"], reverse=True)
        lines.append("model_breakdown:")
        for model_name, stats in sorted_models:
            if stats["calls"] > 0:
                calls = int(stats["calls"])
                pt = int(stats["prompt_tokens"])
                ct = int(stats["completion_tokens"])
                lines.append(f"  {model_name}: {calls} calls, {pt:,}p/{ct:,}c tok")

    lines.append(f"last_owner_message_at: {state.last_owner_message_at or '-'}")
    lines.append(f"timeouts: soft={soft_timeout_sec}s, hard={hard_timeout_sec}s")
    return "\n".join(lines)


def rotate_chat_log_if_needed(drive_root: pathlib.Path, max_bytes: int = 800_000) -> None:
    """Legacy no-op: owner chat lives in runtime.sqlite3 (export via store.export_messages_jsonl)."""
    del drive_root, max_bytes
