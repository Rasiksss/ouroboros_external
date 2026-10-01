"""Single-process task executor for the supervisor runtime."""

from __future__ import annotations

import logging
import pathlib
import queue as queue_mod
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from new_core.loop import CancellationSignal
from ouroboros.agent.core import OuroborosAgent
from ouroboros.agent.interfaces import ForegroundPressure
from ouroboros.observability.event_record import record_event
from ouroboros.observability.log_context import bind_log_context
from ouroboros.storage.runtime_backend import RuntimeStore
from ouroboros.storage.runtime_models import TaskRecord, TaskStatus
from ouroboros.utils import utc_now_iso
from supervisor.agent_factory import AgentFactory
from supervisor.cancellation import EventCancellation
from supervisor.queue import TaskQueue, _queue_lock, parse_iso_to_ts

log = logging.getLogger(__name__)


_FOREGROUND_WORKER_ID = 0
_MEMORY_WORKER_ID = 1
_MEMORY_DISTILLATION_TASK_TYPES = frozenset({"memory_distillation"})
_CONTEXT_CONSOLIDATION_TASK_TYPES = frozenset({"context_consolidation"})
_MEMORY_TASK_TYPES = _MEMORY_DISTILLATION_TASK_TYPES | _CONTEXT_CONSOLIDATION_TASK_TYPES
_CONTEXT_CONSOLIDATION_MAX_DEFER_SEC = 600
_ACTIVE_TASK_STATUSES = ("queued", "scheduled", "interrupted", "running")

# How long a slot waits for a stopped body before giving up on it: above one round's tool
# deadline (OUROBOROS_TOOL_TIMEOUT_SEC, 600s), so a slow tool call still gets to return.
_STOP_GRACE_SECONDS = 900.0

# One thread per lane: foreground and memory.
_POOL_MAX_WORKERS = 2


_warned_unparseable_created_at: set[str] = set()


def _context_consolidation_overdue(record: Any) -> bool:
    if record.task_type != "context_consolidation":
        return False
    created_at = parse_iso_to_ts(record.created_at)
    if created_at is None:
        # Treat as overdue rather than silently disabling the override; warn
        # once per task since this check runs on every scheduler tick.
        task_id = str(record.task_id or "")
        if task_id not in _warned_unparseable_created_at:
            if len(_warned_unparseable_created_at) > 128:
                _warned_unparseable_created_at.clear()
            _warned_unparseable_created_at.add(task_id)
            log.warning(
                "context_consolidation task %s has unparseable created_at=%r; treating as overdue",
                record.task_id,
                record.created_at,
            )
        return True
    return time.time() - created_at >= _CONTEXT_CONSOLIDATION_MAX_DEFER_SEC


class _ConsolidationForegroundPressure(ForegroundPressure):
    """Foreground work defers a consolidation only until the task itself is overdue."""

    def __init__(self, executor: TaskExecutor, record: Any) -> None:
        self._executor = executor
        self._record = record

    def is_pending(self) -> bool:
        return not _context_consolidation_overdue(self._record) and self._executor._foreground_work_exists()


@dataclass
class WorkerSlot:
    worker_id: int
    busy_task_id: str | None = None
    future: Future | None = None
    #: Stop signal for the task in this slot, replaced by a fresh one on every claim.
    cancellation: EventCancellation = field(default_factory=EventCancellation)
    #: Monotonic time the stop was requested, which starts the grace period.
    stop_requested_at: float | None = None

    @property
    def wid(self) -> int:
        return self.worker_id

    def request_stop(self) -> None:
        """Ask this slot's task to stop and start the grace-period clock.

        The clock is set once: a second request must not push the deadline out.
        """
        self.cancellation.request_stop()
        if self.stop_requested_at is None:
            self.stop_requested_at = time.monotonic()

    def clear_task(self) -> None:
        """Drop every per-task handle, so the slot is free for the next claim."""
        self.busy_task_id = None
        self.future = None
        self.cancellation = EventCancellation()
        self.stop_requested_at = None


class TaskExecutor:
    """Claims durable tasks from the store and runs foreground + memory lanes."""

    def __init__(
        self,
        *,
        repo_dir: pathlib.Path,
        drive_root: pathlib.Path,
        task_queue: TaskQueue,
        store: RuntimeStore,
    ) -> None:
        self.repo_dir = pathlib.Path(repo_dir)
        self.drive_root = pathlib.Path(drive_root)
        self.task_queue = task_queue
        self.store = store
        self._event_q: queue_mod.Queue[dict[str, Any]] = queue_mod.Queue()
        self._media_senders: dict[str, Any] = {}
        self._pool: ThreadPoolExecutor | None = None
        self._pool_lock = threading.RLock()
        self._slots: dict[int, WorkerSlot] = {}
        self._agent_factory = AgentFactory(self.repo_dir, self.drive_root, self._event_q, self._media_senders)
        self._agent_lock = threading.Lock()
        self._last_memory_task_type = ""
        for record in self.store.list_tasks(statuses=["running"]):
            if record.task_type == "context_consolidation":
                self.store.upsert_task(record.model_copy(update={"status": "interrupted", "worker_id": None}))

    def get_event_q(self) -> queue_mod.Queue[dict[str, Any]]:
        return self._event_q

    def get_agent(self) -> OuroborosAgent:
        return self._agent_factory.get()

    def set_media_senders(self, *, image: Any, file: Any) -> None:
        if self._pool is not None:
            raise RuntimeError("media senders cannot change after the executor starts")
        self._media_senders = {"image": image, "file": file}
        self._agent_factory.set_media_senders(self._media_senders)

    def all_slots(self) -> list[WorkerSlot]:
        return list(self._slots.values())

    def worker_slots_dict(self) -> dict[int, WorkerSlot]:
        return dict(self._slots)

    def get_slot(self, worker_id: int) -> WorkerSlot | None:
        return self._slots.get(worker_id)

    def get_slot_for_task(self, task_id: str) -> WorkerSlot | None:
        for slot in self._slots.values():
            if slot.busy_task_id == task_id:
                return slot
        return None

    def ensure_started(self) -> None:
        with self._pool_lock:
            if self._pool is None:
                self._agent_factory = AgentFactory(self.repo_dir, self.drive_root, self._event_q, self._media_senders)
                self._pool = ThreadPoolExecutor(max_workers=_POOL_MAX_WORKERS, thread_name_prefix="ouroboros-task")
                self._slots.clear()
                self._slots[_FOREGROUND_WORKER_ID] = WorkerSlot(worker_id=_FOREGROUND_WORKER_ID)
                log.info(
                    "Supervisor task executor pool started drive_root=%s repo_dir=%s",
                    self.drive_root,
                    self.repo_dir,
                )
                record_event(
                    self.drive_root,
                    "supervisor",
                    "single_process_executor_start",
                    {"ts": utc_now_iso(), "count": 1},
                )

    def shutdown(
        self,
        *,
        result_reason: str = "Task was stopped by supervisor shutdown.",
        result_status: str = "failed",
    ) -> None:
        terminal_status = result_status or "failed"
        with _queue_lock:
            for task_id, meta in self.task_queue.list_running().items():
                if meta.record.task_type == "context_consolidation":
                    self.store._commit_context_consolidation(
                        meta.record,
                        status="interrupted",
                        task_input=meta.record.input,
                        result=result_reason,
                    )
                    continue
                if terminal_status == "cancelled":
                    self.store.cancel_task(task_id, result=result_reason)
                else:
                    self.store.fail_task(task_id, result=result_reason, status=terminal_status)  # type: ignore[arg-type]
            for record in self.task_queue.list_pending_records():
                if record.task_type == "context_consolidation":
                    continue
                if terminal_status == "cancelled":
                    self.store.cancel_task(record.task_id, result=result_reason)
                else:
                    self.store.fail_task(record.task_id, result=result_reason, status=terminal_status)  # type: ignore[arg-type]
            # Shutting down ends every running task through the same per-task signal a single
            # kill uses, and only now that the records are terminal: a body that returns in
            # this window has to find its task already closed. Each body holds the signal it
            # was handed, so clearing the slot right after does not take the stop back.
            for slot in self._slots.values():
                slot.request_stop()
                slot.clear_task()
        with self._pool_lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def assign_next(self) -> None:
        with _queue_lock:
            self.ensure_started()
            self._assign_slot(
                self._slots.setdefault(_FOREGROUND_WORKER_ID, WorkerSlot(worker_id=_FOREGROUND_WORKER_ID)),
                exclude_task_types=_MEMORY_TASK_TYPES,
            )
            memory_slot = self._slots.get(_MEMORY_WORKER_ID)
            has_memory_work = any(self.task_queue.queue_has_task_type(task_type) for task_type in _MEMORY_TASK_TYPES)
            if memory_slot is not None or has_memory_work:
                self._assign_memory_slot(
                    self._slots.setdefault(_MEMORY_WORKER_ID, WorkerSlot(worker_id=_MEMORY_WORKER_ID))
                )

    def _assign_memory_slot(self, slot: WorkerSlot) -> None:
        active = self.store.list_tasks(statuses=_ACTIVE_TASK_STATUSES)
        foreground_work_exists = self._foreground_work_exists(active)
        overdue = any(record.status != "running" and _context_consolidation_overdue(record) for record in active)
        if not foreground_work_exists:
            preferences = [_MEMORY_TASK_TYPES]
        elif overdue:
            # An overdue consolidation becomes eligible alongside foreground work,
            # but alternates with distillation so neither lane starves: pending
            # distillations are capped and dropped past _MAX_PENDING_DISTILLATIONS.
            preferences = [_CONTEXT_CONSOLIDATION_TASK_TYPES, _MEMORY_DISTILLATION_TASK_TYPES]
            if self._last_memory_task_type == "context_consolidation":
                preferences.reverse()
        else:
            preferences = [_MEMORY_DISTILLATION_TASK_TYPES]
        for include_task_types in preferences:
            claimed = self._assign_slot(slot, include_task_types=include_task_types)
            if claimed is not None:
                self._last_memory_task_type = str(claimed.task_type or "")
                return

    def _foreground_work_exists(self, active: list[TaskRecord] | None = None) -> bool:
        if active is None:
            active = self.store.list_tasks(statuses=_ACTIVE_TASK_STATUSES)
        return any(record.task_type not in _MEMORY_TASK_TYPES for record in active)

    def ensure_healthy(self) -> None:
        with _queue_lock:
            for slot in self._slots.values():
                if slot.future is None:
                    continue
                if slot.future.done():
                    self._finish_slot_future(slot)
                else:
                    self._release_if_abandoned(slot)

    def _reset_agent_runtime(self) -> None:
        """Hand the next foreground task a fresh lock and agent.

        The abandoned thread keeps the pair it is already inside, so it can hold its lock and
        mutate its agent for as long as it lives without blocking or corrupting the next task.
        """
        self._agent_lock = threading.Lock()
        self._agent_factory = AgentFactory(self.repo_dir, self.drive_root, self._event_q, self._media_senders)

    def _replace_pool(self) -> None:
        """Hand the remaining slots a fresh pool, since the abandoned thread keeps one of its own.

        Without this the leak costs the runtime one of its two workers for good, and the next
        claim only queues behind it — running, heartbeatless, until the sweep kills it. The old
        pool is shut down without waiting, so its queued items still run and its idle threads exit.
        """
        with self._pool_lock:
            old_pool = self._pool
            if old_pool is None:
                return
            self._pool = ThreadPoolExecutor(max_workers=_POOL_MAX_WORKERS, thread_name_prefix="ouroboros-task")
            old_pool.shutdown(wait=False)

    def _release_if_abandoned(self, slot: WorkerSlot) -> None:
        """Free a slot whose body has not honoured its stop signal within the grace period.

        A ``task_abandoned`` event is a real leak: the thread is still running.
        """
        requested_at = slot.stop_requested_at
        if requested_at is None or slot.busy_task_id is None:
            return
        elapsed = time.monotonic() - requested_at
        if elapsed < _STOP_GRACE_SECONDS:
            return
        task_id = slot.busy_task_id
        log.warning(
            "Task %s did not stop %ds after the stop signal; releasing worker %d",
            task_id,
            int(elapsed),
            slot.worker_id,
        )
        record_event(
            self.drive_root,
            "supervisor",
            "task_abandoned",
            {
                "ts": utc_now_iso(),
                "task_id": task_id,
                "worker_id": slot.worker_id,
                "elapsed_sec": round(elapsed, 2),
                "grace_sec": _STOP_GRACE_SECONDS,
            },
        )
        # The future is left alone: the thread cannot be killed and reading its result here
        # would block the tick, so dropping the handle is all the slot can do.
        self._replace_pool()
        if slot.worker_id == _FOREGROUND_WORKER_ID:
            # Only this lane takes the agent lock, and the leaked thread still holds it.
            # Reusing the slot over that lock is the cascade this whole path exists to end.
            self._reset_agent_runtime()
        slot.clear_task()

    def _assign_slot(
        self,
        slot: WorkerSlot,
        *,
        include_task_types: frozenset[str] | None = None,
        exclude_task_types: frozenset[str] | None = None,
    ) -> TaskRecord | None:
        """Claim and start the next matching task; return its record once started."""
        if slot.busy_task_id is not None:
            if slot.future is not None and slot.future.done():
                self._finish_slot_future(slot)
            else:
                return None
        record = self.task_queue.claim_next(
            worker_id=slot.worker_id,
            include_task_types=include_task_types,
            exclude_task_types=exclude_task_types,
        )
        if record is None:
            return None
        task_id = record.task_id
        if not task_id:
            return None
        slot.busy_task_id = task_id
        slot.cancellation = EventCancellation()
        slot.stop_requested_at = None
        pool = self._pool
        if pool is None:
            # Shutdown race: claimed but never started — don't report it as started.
            slot.clear_task()
            return None
        # The signal is read here, not inside the worker thread: by the time that thread runs
        # the slot may already have been released and re-claimed with a fresh one.
        slot.future = pool.submit(self._run_task, record, slot.cancellation)
        return record

    def _run_task(self, record: Any, cancellation: CancellationSignal) -> None:
        if record.task_type == "memory_distillation":
            self._run_memory_distillation(record)
            return
        if record.task_type == "context_consolidation":
            self._run_context_consolidation(record, cancellation)
            return
        task = record.to_agent_input()
        try:
            task_id = str(record.task_id or "")
            correlation_id = str(task.get("correlation_id") or task_id or "")
            worker_id = int(record.worker_id if record.worker_id is not None else _FOREGROUND_WORKER_ID)
            with bind_log_context(task_id=task_id, correlation_id=correlation_id), self._agent_lock:
                agent = self.get_agent()
                events = agent.handle_task(task, cancellation=cancellation)
            for event in events:
                event_out = dict(event)
                event_out["worker_id"] = worker_id
                self._event_q.put(event_out)
        except Exception as exc:
            task_id = str(record.task_id or "")
            worker_id = int(record.worker_id if record.worker_id is not None else _FOREGROUND_WORKER_ID)
            self._log_worker_crash(worker_id, "handle_task", exc, "")
            self._write_failure_result(
                task_id,
                reason=f"Task failed in single-process executor: {type(exc).__name__}: {exc}",
            )
            self._event_q.put(
                {
                    "type": "task_done",
                    "task_id": task_id,
                    "task_type": record.task_type,
                    "worker_id": worker_id,
                    "status": "failed",
                }
            )

    def _run_memory_distillation(self, record: Any) -> None:
        """Run a memory_distillation task without the full agent loop.

        Routes through the same _event_q -> task_done/llm_usage path as regular
        tasks, so lifecycle, usage and error accounting work without changes to
        supervisor/events.py.
        """
        from ouroboros.agent.core import Env
        from ouroboros.agent.task_pipeline import run_memory_distillation_task
        from ouroboros.llm import LLMClient

        task_id = str(record.task_id or "")
        worker_id = int(record.worker_id if record.worker_id is not None else _MEMORY_WORKER_ID)
        try:
            with bind_log_context(task_id=task_id, correlation_id=task_id):
                env = Env(repo_dir=self.repo_dir, drive_root=self.drive_root)
                llm = LLMClient()
                result = run_memory_distillation_task(
                    env,
                    llm,
                    dict(record.input or {}),
                    event_queue=self._event_q,
                    task_id=task_id,
                )
        except Exception as exc:
            self._log_worker_crash(worker_id, "memory_distillation", exc, "")
            self._write_failure_result(
                task_id,
                reason=f"Memory distillation failed: {type(exc).__name__}: {exc}",
            )
            self._event_q.put(
                {
                    "type": "task_done",
                    "task_id": task_id,
                    "task_type": record.task_type,
                    "worker_id": worker_id,
                    "status": "failed",
                    "result": f"{type(exc).__name__}: {exc}",
                }
            )
            return

        result_text = (
            "SAVED: " + ", ".join(result.saved_names) if result.saved_count else result.error or "NOTHING_TO_SAVE"
        )
        self._event_q.put(
            {
                "type": "task_done",
                "task_id": task_id,
                "task_type": record.task_type,
                "worker_id": worker_id,
                "status": result.status,
                "result": result_text,
                "total_rounds": result.rounds,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
            }
        )

    def _run_context_consolidation(self, record: Any, cancellation: CancellationSignal) -> None:
        from ouroboros.agent.context_consolidation import run_context_consolidation_task
        from ouroboros.agent.core import Env
        from ouroboros.llm import LLMClient

        task_id = str(record.task_id or "")
        try:
            with bind_log_context(task_id=task_id, correlation_id=task_id):
                continuation, summary, detail = run_context_consolidation_task(
                    Env(repo_dir=self.repo_dir, drive_root=self.drive_root),
                    LLMClient(),
                    dict(record.input or {}),
                    cancellation=cancellation,
                    foreground_pending=_ConsolidationForegroundPressure(self, record),
                    event_queue=self._event_q,
                    task_id=task_id,
                )
            status: TaskStatus = "interrupted" if continuation is not None else "completed"
            task_input = (
                record.input
                if continuation is None
                else {
                    "id": task_id,
                    "type": "context_consolidation",
                    "source": "internal",
                    "scheduled_tail_message_id": record.input.get("scheduled_tail_message_id", ""),
                    **continuation,
                }
            )
            committed = self.store._commit_context_consolidation(
                record,
                status=status,
                task_input=task_input,
                result=detail,
                expected_cursor=str(record.input.get("expected_cursor") or ""),
                summary=summary,
            )
            if not committed:
                if summary is not None:
                    self.store._commit_context_consolidation(
                        record, status="interrupted", task_input=record.input, result="STALE_CURSOR"
                    )
                return
            if continuation is not None:
                return
        except Exception as exc:
            self._log_worker_crash(_MEMORY_WORKER_ID, "context_consolidation", exc, "")
            status = "failed"
            detail = f"{type(exc).__name__}: {exc}"
            if not self.store._commit_context_consolidation(record, status=status, result=detail):
                return

        event = {"type": "task_done", "task_id": task_id, "task_type": record.task_type}
        event.update(worker_id=_MEMORY_WORKER_ID, status=status, result=detail)
        self._event_q.put(event)
        self.task_queue.record_consolidation_outcome(status)
        # A failed run schedules nothing, or failure becomes a retry loop; the messages that
        # arrived meanwhile re-trigger scheduling from message_bus on their own.
        if status != "completed":
            return
        try:
            from ouroboros.storage.settings.store import load_settings

            # Forced: this run consumed part of the backlog without the chat tail moving.
            self.task_queue.schedule_context_consolidation(load_settings(), force=True)
        except Exception:
            log.warning("Failed to reschedule durable context consolidation", exc_info=True)

    def _finish_slot_future(self, slot: WorkerSlot) -> None:
        task_id = slot.busy_task_id
        future = slot.future
        slot.clear_task()
        if future is not None:
            try:
                future.result()
            except Exception as exc:
                if task_id:
                    self._write_failure_result(
                        task_id,
                        reason=f"Task future failed: {type(exc).__name__}: {exc}",
                    )

    def _write_failure_result(
        self,
        task_id: str,
        reason: str = "Task was not completed.",
        status: str = "",
    ) -> None:
        if not task_id:
            return
        try:
            from ouroboros.storage.task_results import STATUS_FAILED, write_task_result

            write_task_result(
                self.drive_root,
                task_id,
                status or STATUS_FAILED,
                result=reason,
                total_rounds=0,
            )
        except Exception:
            log.warning("Failed to write failure result for task %s", task_id, exc_info=True)

    def _log_worker_crash(self, wid: int, phase: str, exc: Exception, tb: str) -> None:
        try:
            record_event(
                self.drive_root,
                "supervisor",
                "executor_task_error",
                {
                    "ts": utc_now_iso(),
                    "worker_id": wid,
                    "phase": phase,
                    "error": repr(exc),
                    "traceback": str(tb)[:3000],
                },
            )
        except Exception:
            log.debug("Suppressed executor crash log failure", exc_info=True)


class Worker(WorkerSlot):
    """Alias for tests and legacy imports."""

    def __init__(
        self,
        wid: int | None = None,
        *,
        worker_id: int | None = None,
        busy_task_id: str | None = None,
        future: Future | None = None,
    ) -> None:
        resolved_id = worker_id if worker_id is not None else (wid if wid is not None else 0)
        super().__init__(worker_id=resolved_id, busy_task_id=busy_task_id, future=future)


def get_task_executor() -> TaskExecutor:
    from supervisor.runtime_context import require_runtime

    return require_runtime().executor


def kill_workers(
    force: bool = True,
    *,
    result_reason: str = "Task was stopped by supervisor shutdown.",
    result_status: str = "",
) -> None:
    del force
    from supervisor.runtime_context import try_get_runtime

    runtime = try_get_runtime()
    if runtime is None:
        return
    runtime.executor.shutdown(result_reason=result_reason, result_status=result_status)


def spawn_workers(n: int = 0) -> None:
    del n
    get_task_executor().ensure_started()


def respawn_worker(wid: int) -> None:
    del wid
    get_task_executor().ensure_started()


def assign_tasks() -> None:
    get_task_executor().assign_next()


def ensure_workers_healthy() -> None:
    get_task_executor().ensure_healthy()


def get_event_q() -> queue_mod.Queue[dict[str, Any]]:
    return get_task_executor().get_event_q()


def _get_chat_agent():
    return get_task_executor().get_agent()


def _write_failure_result(
    task_id: str,
    reason: str = "Task was not completed.",
    status: str = "",
) -> None:
    get_task_executor()._write_failure_result(task_id, reason=reason, status=status)


def init(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    max_workers: int,
    soft_timeout: int,
    hard_timeout: int,
    total_budget_limit: float = 0.0,
) -> TaskExecutor:
    """Deprecated: use build_supervisor_runtime and set_runtime."""
    del max_workers, total_budget_limit
    from supervisor.bootstrap import build_supervisor_runtime
    from supervisor.runtime_context import set_runtime

    runtime = build_supervisor_runtime(
        settings={
            "OUROBOROS_SOFT_TIMEOUT_SEC": soft_timeout,
            "OUROBOROS_HARD_TIMEOUT_SEC": hard_timeout,
        },
        drive_root=pathlib.Path(drive_root),
        repo_dir=pathlib.Path(repo_dir),
        bridge=None,
    )
    set_runtime(runtime)
    return runtime.executor


def handle_chat_direct(
    chat_id: int,
    text: str,
    image_data: tuple[str, str] | tuple[str, str, str] | None = None,
    task_constraint: dict | None = None,
) -> None:
    """Deprecated: submit via TaskOrchestrator."""
    from supervisor.orchestrator.envelope import InboundEnvelope
    from supervisor.runtime_context import try_get_orchestrator

    orchestrator = try_get_orchestrator()
    if orchestrator is None:
        raise RuntimeError("Supervisor orchestrator is not initialized")
    orchestrator.submit(
        InboundEnvelope.from_chat(
            chat_id=chat_id,
            user_id=chat_id,
            text=text,
            source="dialogs",
            task_constraint=task_constraint,
            image_data=image_data,
        )
    )


__all__ = [
    "TaskExecutor",
    "Worker",
    "WorkerSlot",
    "assign_tasks",
    "ensure_workers_healthy",
    "get_event_q",
    "get_task_executor",
    "handle_chat_direct",
    "init",
    "kill_workers",
    "respawn_worker",
    "spawn_workers",
]
