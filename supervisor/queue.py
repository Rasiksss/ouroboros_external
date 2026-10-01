"""Supervisor task queue backed by the runtime metadata store."""

from __future__ import annotations

import datetime
import logging
import pathlib
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ouroboros.observability.event_record import record_event
from ouroboros.storage.chat_store import SHARED_CHAT_SUMMARY_CONVERSATION_ID
from ouroboros.storage.conversation_format import format_chat_summary_line
from ouroboros.storage.runtime_backend import MessageAnchorMissingError, RuntimeStore
from ouroboros.storage.runtime_models import TaskRecord, TaskRequest
from ouroboros.storage.runtime_store import get_runtime_store, init_runtime_store
from ouroboros.storage.settings.schema import SettingsLike
from ouroboros.utils import estimate_tokens, utc_now_iso
from supervisor.message_bus import send_outbound_message
from supervisor.state import RuntimeStateRepository, get_state_repository

log = logging.getLogger(__name__)

HEARTBEAT_STALE_SEC: int = 120
QUEUE_MAX_RETRIES: int = 1

# A safety valve on a retry loop, not a tuning surface, so not a setting.
_CONSOLIDATION_FAILURE_COOLDOWN_BASE_SEC: int = 300
_CONSOLIDATION_FAILURE_COOLDOWN_CAP_SEC: int = 3600

_queue_lock = threading.RLock()

#: Chat tail seen when the scheduling probe last declined for lack of backlog, per drive root.
#: Module-level because the probe's callers build a fresh TaskQueue per call.
_last_probed_chat_tail: dict[str, str] = {}


@dataclass
class RunningTaskMeta:
    record: TaskRecord
    worker_id: int
    started_at: float
    last_heartbeat_at: float
    soft_sent: bool
    attempt: int

    @property
    def task_id(self) -> str:
        return self.record.task_id


def _default_task_priority(task_type: str) -> int:
    if str(task_type or "").strip().lower() == "task":
        return 0
    return 1


def _consolidation_failure_record(record: Any) -> tuple[int, float]:
    """Consecutive failures and the last failure timestamp; absent state reads as zero."""
    if not isinstance(record, dict):
        return 0, 0.0
    try:
        consecutive = max(0, int(record.get("consecutive") or 0))
    except (TypeError, ValueError):
        return 0, 0.0
    return consecutive, parse_iso_to_ts(str(record.get("last_failure_at") or "")) or 0.0


def _consolidation_failure_cooldown_sec(consecutive: int) -> float:
    if consecutive <= 0:
        return 0.0
    # Shift bounded so an absurd stored count cannot build a giant int before min().
    grown = _CONSOLIDATION_FAILURE_COOLDOWN_BASE_SEC * 2 ** min(consecutive - 1, 32)
    return float(min(grown, _CONSOLIDATION_FAILURE_COOLDOWN_CAP_SEC))


class TaskQueue:
    """Durable task queue over the runtime metadata store."""

    def __init__(
        self,
        store: RuntimeStore,
        drive_root: pathlib.Path,
        *,
        soft_timeout: int = 600,
        hard_timeout: int = 1800,
        state_repo: RuntimeStateRepository | None = None,
    ) -> None:
        self.store = store
        self.drive_root = pathlib.Path(drive_root)
        self.soft_timeout_sec = int(soft_timeout)
        self.hard_timeout_sec = int(hard_timeout)
        self.state_repo = state_repo or get_state_repository(self.drive_root)

    def refresh_timeouts_from_settings(self, settings: SettingsLike) -> None:
        soft_raw = settings.get("OUROBOROS_SOFT_TIMEOUT_SEC")
        if soft_raw is not None:
            try:
                self.soft_timeout_sec = int(soft_raw)
            except (TypeError, ValueError):
                pass
        hard_raw = settings.get("OUROBOROS_HARD_TIMEOUT_SEC")
        if hard_raw is not None:
            try:
                self.hard_timeout_sec = int(hard_raw)
            except (TypeError, ValueError):
                pass

    def _allocate_priority(self, base: int, *, front: bool) -> int:
        if not front:
            return base
        pending = self.store.list_tasks(statuses=["queued", "scheduled", "interrupted"])
        if not pending:
            return base - 1000
        return min(int(record.priority) for record in pending) - 1000

    def enqueue(self, request: TaskRequest, *, front: bool = False) -> TaskRecord:
        payload = request.model_copy(deep=True)
        if not payload.id:
            payload.id = uuid.uuid4().hex[:8]
        with _queue_lock:
            existing = self.store.get_task(payload.id)
            if existing is not None and existing.status != "requested":
                return existing
            base_priority = int(payload.priority) if payload.priority else _default_task_priority(payload.type)
            payload.priority = self._allocate_priority(base_priority, front=front)
            if not payload.queued_at:
                payload.queued_at = utc_now_iso()
            if existing is None:
                return self.store.create_task(payload, status="queued")
            scheduled = existing.model_copy(
                update={
                    "conversation_id": payload.conversation_id,
                    "source": payload.source,
                    "correlation_id": payload.correlation_id,
                    "status": "scheduled",
                    "task_type": payload.type,
                    "chat_id": payload.chat_id,
                    "input": payload.to_task_dict(),
                    "result": "",
                    "priority": payload.priority,
                    "attempt": max(1, int(payload.attempt or 1)),
                    "queued_at": payload.queued_at,
                }
            )
            return self.store.upsert_task(scheduled)

    def enqueue_dict(self, task: dict[str, Any], *, front: bool = False) -> TaskRecord:
        return self.enqueue(TaskRequest.model_validate(task), front=front)

    def claim_next(
        self,
        worker_id: int = 0,
        *,
        include_task_types: Iterable[str] | None = None,
        exclude_task_types: Iterable[str] | None = None,
    ) -> TaskRecord | None:
        return self.store.claim_next_task(
            worker_id=worker_id,
            include_task_types=include_task_types,
            exclude_task_types=exclude_task_types,
        )

    def queue_has_task_type(self, task_type: str) -> bool:
        wanted = str(task_type or "")
        for record in self.store.list_tasks(statuses=["queued", "scheduled", "interrupted", "running"]):
            if str(record.task_type or "") == wanted:
                return True
        return False

    def record_consolidation_outcome(self, status: str) -> None:
        """Count consecutive consolidation failures; a completed run clears them."""
        outcome = str(status or "")
        if outcome not in ("completed", "failed"):
            return
        try:
            # Through the repository, not a raw owner-state dump: the counter shares its row
            # with the usage totals, and a whole-row write from here would revert them.
            with self.state_repo.mutate() as state:
                if outcome == "completed":
                    state.context_consolidation_failures = {"consecutive": 0, "last_failure_at": ""}
                else:
                    consecutive, _ = _consolidation_failure_record(state.context_consolidation_failures)
                    state.context_consolidation_failures = {
                        "consecutive": consecutive + 1,
                        "last_failure_at": utc_now_iso(),
                    }
        except Exception:
            log.warning("Failed to record context consolidation outcome %s", outcome, exc_info=True)

    def _consolidation_cooldown(self) -> tuple[float, int]:
        """Remaining cooldown in seconds and the consecutive-failure count behind it."""
        try:
            failures = self.state_repo.load().context_consolidation_failures
        except Exception:
            log.warning("Failed to read context consolidation failure state", exc_info=True)
            return 0.0, 0
        consecutive, last_failure_ts = _consolidation_failure_record(failures)
        if consecutive <= 0 or last_failure_ts <= 0:
            return 0.0, consecutive
        remaining = _consolidation_failure_cooldown_sec(consecutive) - (time.time() - last_failure_ts)
        return max(0.0, remaining), consecutive

    def schedule_context_consolidation(self, settings: Any, *, force: bool = False) -> bool:
        """Enqueue a consolidation when the chat backlog warrants one.

        Every persisted message asks, so an unmoved chat tail short-circuits the backlog
        queries below: no new message means no new backlog. ``force`` skips that gate for the
        caller that has just consumed part of the backlog and needs the next chunk scheduled.
        """
        with _queue_lock:
            remaining_cooldown, consecutive_failures = self._consolidation_cooldown()
            if remaining_cooldown > 0:
                # Every persisted message asks, so this repeats for the whole cooldown.
                log.info(
                    "Context consolidation not scheduled: %d consecutive failures, %ds of cooldown left",
                    consecutive_failures,
                    int(remaining_cooldown),
                )
                return False
            latest = self.store.list_chat_history_messages(limit=1)
            tail_id = str(latest[0].message_id) if latest else ""
            gate_key = str(self.drive_root)
            if not force and tail_id and _last_probed_chat_tail.get(gate_key) == tail_id:
                return False
            committed = self.store.get_latest_context_summary(SHARED_CHAT_SUMMARY_CONVERSATION_ID)
            cursor = str(committed.source_offsets_ref or "") if committed else ""
            missing = bool(cursor and self.store.get_message(cursor) is None)
            message_limit = int(settings.OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES)
            token_limit = int(settings.OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS)
            if message_limit <= 0 or token_limit <= 0:
                raise ValueError("chat consolidation thresholds must be positive")
            try:
                messages = self.store.list_chat_history_messages(
                    limit=message_limit, after_message_id=cursor or None, oldest_first=True
                )
            except MessageAnchorMissingError:
                missing = True
                messages = self.store.list_chat_history_messages(limit=message_limit, oldest_first=True)
            tokens = sum(estimate_tokens(format_chat_summary_line(message)) + 6 for message in messages)
            if not missing and len(messages) < message_limit and tokens < token_limit:
                _last_probed_chat_tail[gate_key] = tail_id
                return False
            if self.queue_has_task_type("context_consolidation"):
                return False
            self.enqueue_dict(
                {
                    "type": "context_consolidation",
                    "source": "internal",
                    "expected_cursor": cursor,
                    "scheduled_tail_message_id": tail_id,
                }
            )
            return True

    def list_pending_records(self) -> list[TaskRecord]:
        return self.store.list_tasks(statuses=["queued", "scheduled", "interrupted"])

    def list_pending_tasks(self) -> list[dict[str, Any]]:
        return [dict(record.input) for record in self.list_pending_records()]

    def list_running(self) -> dict[str, RunningTaskMeta]:
        running: dict[str, RunningTaskMeta] = {}
        for record in self.store.list_tasks(statuses=["running"]):
            started_ts = parse_iso_to_ts(record.started_at) or 0.0
            hb_ts = parse_iso_to_ts(record.heartbeat_at) or started_ts
            running[record.task_id] = RunningTaskMeta(
                record=record,
                worker_id=int(record.worker_id or 0),
                started_at=started_ts,
                last_heartbeat_at=hb_ts,
                soft_sent=bool(record.metadata.get("soft_sent")),
                attempt=int(record.attempt or 1),
            )
        return running

    def list_running_tasks(self) -> dict[str, dict[str, Any]]:
        """Legacy shape for status_text and older callers."""
        legacy: dict[str, dict[str, Any]] = {}
        for task_id, meta in self.list_running().items():
            legacy[task_id] = {
                "task": dict(meta.record.input),
                "worker_id": meta.worker_id,
                "started_at": meta.started_at,
                "last_heartbeat_at": meta.last_heartbeat_at,
                "soft_sent": meta.soft_sent,
                "attempt": meta.attempt,
            }
        return legacy

    def drain_all_pending(self) -> list[TaskRecord]:
        drained: list[TaskRecord] = []
        for record in self.list_pending_records():
            drained.append(record)
            self.store.fail_task(record.task_id, result="Task drained during supervisor shutdown.")
        return drained

    def cancel_task_by_id(self, task_id: str, executor: Any | None = None) -> bool:
        with _queue_lock:
            for record in self.store.list_tasks(statuses=["queued", "scheduled", "interrupted"]):
                if record.task_id != task_id:
                    continue
                self.store.cancel_task(task_id, result="Task cancelled by user/agent request.")
                return True
            if executor is not None:
                slot = executor.get_slot_for_task(task_id)
                if slot is not None:
                    self.store.cancel_task(task_id, result="Running task cancelled.")
                    future = slot.future
                    if future is not None:
                        # Only ends a future the pool has not started; the signal ends the rest.
                        future.cancel()
                    slot.request_stop()
                    return True
        return False

    def enforce_task_timeouts(self, executor: Any) -> None:
        running = self.list_running()
        if not running:
            return

        owner_state = self.state_repo.load()
        now = time.time()

        for task_id, meta in list(running.items()):
            started_at = float(meta.started_at or 0.0)
            if started_at <= 0:
                continue
            last_hb = float(meta.last_heartbeat_at or started_at)
            runtime_sec = max(0.0, now - started_at)
            hb_lag_sec = max(0.0, now - last_hb)
            worker_id = int(meta.worker_id or 0)
            task_type = str(meta.record.task_type or "")
            attempt = int(meta.attempt or 1)
            task_input = meta.record.to_agent_input()
            route_chat_id = meta.record.chat_id
            if route_chat_id in (None, ""):
                route_chat_id = owner_state.owner_chat_id
            route_source = str(meta.record.source or owner_state.owner_chat_source or "")
            route_transport = task_input.get("transport")
            route_transport = dict(route_transport) if isinstance(route_transport, dict) else {}

            if runtime_sec >= self.soft_timeout_sec and not meta.soft_sent:
                record = self.store.get_task(task_id)
                if record is not None:
                    record.metadata["soft_sent"] = True
                    self.store.upsert_task(record)
                if route_chat_id not in (None, ""):
                    self._notify_owner(
                        route_chat_id,
                        f"⏱️ Task {task_id} running for {int(runtime_sec)}s. "
                        f"type={task_type}, heartbeat_lag={int(hb_lag_sec)}s. Continuing.",
                        source=route_source,
                        transport=route_transport,
                    )

            if runtime_sec < self.hard_timeout_sec:
                continue

            # The attempt is spent on the record being retried, in the write that fails it, so
            # QUEUE_MAX_RETRIES bounds the chain even if the record is seen overdue again.
            requeued = attempt <= QUEUE_MAX_RETRIES
            new_attempt = attempt + 1 if requeued else attempt
            killed = self.store.fail_task(
                task_id,
                result=f"Task killed by hard timeout after {int(runtime_sec)}s.",
                attempt=new_attempt,
            )
            # The absorbing guard refuses the transition when the body closed the record between
            # the listing and here: that task was never killed, so it is neither retried nor
            # announced as killed.
            if killed is None or killed.status != "failed":
                continue

            # Only after the record is terminal: a body that returns between the two would
            # otherwise find itself still `running` and publish its answer as a normal reply.
            slot = executor.get_slot(worker_id)
            if slot is not None and slot.busy_task_id == task_id:
                future = slot.future
                if future is not None:
                    # Only ends a future the pool has not started; the signal ends the rest.
                    future.cancel()
                # Freeing the slot here is what used to hand the next task to a worker whose
                # thread still held the agent lock, so it stays claimed until the body returns.
                slot.request_stop()
                executor.ensure_started()

            if requeued:
                retried = dict(task_input)
                retried["original_task_id"] = task_id
                retried["id"] = uuid.uuid4().hex[:8]
                retried["_attempt"] = new_attempt
                retried["timeout_retry_from"] = task_id
                retried["timeout_retry_at"] = utc_now_iso()
                self.enqueue_dict(retried, front=True)

            # A kill is a failed run: it counts toward the cooldown and schedules nothing.
            if task_type == "context_consolidation":
                self.record_consolidation_outcome("failed")

            record_event(
                self.drive_root,
                "supervisor",
                "task_hard_timeout",
                {
                    "ts": utc_now_iso(),
                    "task_id": task_id,
                    "task_type": task_type,
                    "worker_id": worker_id,
                    "runtime_sec": round(runtime_sec, 2),
                    "heartbeat_lag_sec": round(hb_lag_sec, 2),
                    "attempt": attempt,
                    "requeued": requeued,
                    "new_attempt": new_attempt,
                    "max_retries": QUEUE_MAX_RETRIES,
                },
            )

            if route_chat_id not in (None, ""):
                tail = (
                    f"Task queued for retry attempt={new_attempt}."
                    if requeued
                    else "Retry limit exhausted, task stopped."
                )
                self._notify_owner(
                    route_chat_id,
                    (
                        f"🛑 Hard-timeout: task {task_id} killed after {int(runtime_sec)}s.\n"
                        f"Worker {worker_id} restarted. {tail}"
                    ),
                    source=route_source,
                    transport=route_transport,
                )

    def _notify_owner(self, chat_id: Any, text: str, *, source: str, transport: dict[str, Any]) -> None:
        """A timeout notice on the owner's real transports; a failed one must not stop the sweep."""
        try:
            send_outbound_message(chat_id, text, source=source, transport=transport)
        except Exception:
            log.warning("Failed to deliver a task timeout notice to chat %s", chat_id, exc_info=True)


def parse_iso_to_ts(iso_ts: str) -> float | None:
    txt = str(iso_ts or "").strip()
    if not txt:
        return None
    try:
        return datetime.datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp()
    except Exception:
        log.debug("Failed to parse ISO timestamp: %s", txt, exc_info=True)
        return None


def _runtime_queue() -> TaskQueue:
    from supervisor.runtime_context import require_runtime

    return require_runtime().task_queue


def get_task_queue(drive_root: pathlib.Path | None = None) -> TaskQueue:
    if drive_root is not None:
        root = pathlib.Path(drive_root)
        store = get_runtime_store(root) or init_runtime_store(root)
        from supervisor.runtime_context import try_get_runtime

        runtime = try_get_runtime()
        soft = runtime.soft_timeout if runtime is not None else 600
        hard = runtime.hard_timeout if runtime is not None else 1800
        state_repo = get_state_repository(root)
        return TaskQueue(store, root, soft_timeout=soft, hard_timeout=hard, state_repo=state_repo)
    return _runtime_queue()


def init(drive_root: pathlib.Path, soft_timeout: int, hard_timeout: int) -> TaskQueue:
    """Create a task queue for tests; production uses build_supervisor_runtime."""
    root = pathlib.Path(drive_root)
    store = init_runtime_store(root)
    return TaskQueue(
        store,
        root,
        soft_timeout=soft_timeout,
        hard_timeout=hard_timeout,
        state_repo=get_state_repository(root),
    )


def refresh_timeouts_from_settings(settings: SettingsLike) -> None:
    _runtime_queue().refresh_timeouts_from_settings(settings)


def enqueue_task(task: dict[str, Any], front: bool = False) -> dict[str, Any]:
    record = _runtime_queue().enqueue_dict(task, front=front)
    return dict(record.input)


def queue_has_task_type(task_type: str) -> bool:
    return _runtime_queue().queue_has_task_type(task_type)


def list_pending_tasks() -> list[dict[str, Any]]:
    return _runtime_queue().list_pending_tasks()


def list_running_tasks() -> dict[str, dict[str, Any]]:
    return _runtime_queue().list_running_tasks()


def drain_all_pending() -> list[dict[str, Any]]:
    return [dict(record.input) for record in _runtime_queue().drain_all_pending()]


def cancel_task_by_id(task_id: str) -> bool:
    from supervisor.runtime_context import require_runtime

    runtime = require_runtime()
    return runtime.task_queue.cancel_task_by_id(task_id, executor=runtime.executor)


def enforce_task_timeouts() -> None:
    from supervisor.runtime_context import require_runtime

    runtime = require_runtime()
    runtime.task_queue.enforce_task_timeouts(runtime.executor)


__all__ = [
    "HEARTBEAT_STALE_SEC",
    "QUEUE_MAX_RETRIES",
    "RunningTaskMeta",
    "TaskQueue",
    "_queue_lock",
    "cancel_task_by_id",
    "drain_all_pending",
    "enforce_task_timeouts",
    "enqueue_task",
    "get_task_queue",
    "init",
    "list_pending_tasks",
    "list_running_tasks",
    "parse_iso_to_ts",
    "queue_has_task_type",
    "refresh_timeouts_from_settings",
]
