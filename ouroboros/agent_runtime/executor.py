"""The slots this instance runs tasks in, and the claim that fills them."""

from __future__ import annotations

import contextvars
import logging
import pathlib
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from new_core.coordination.interfaces import IWorkSignal, TaskStopNotice
from new_core.identity import OwnerScope
from new_core.loop import CancellationSignal
from new_core.skills.catalog import SkillCatalogSnapshot
from new_core.skills.interfaces import IExtensionRuntime
from new_core.skills.surface import SkillAvailability
from new_core.tools.interfaces import ToolOutbound
from ouroboros.agent.core import OuroborosAgent
from ouroboros.agent.interfaces.owner_session import IOwnerSessionRepository
from ouroboros.agent.interfaces.startup_usage import IStartupUsageRepository
from ouroboros.agent_events.interfaces import AgentEvent, IAgentEventPublisher, TaskFinished
from ouroboros.agent_runtime.agent_factory import AgentFactory
from ouroboros.agent_runtime.cancellation import (
    STOP_POLL_INTERVAL_SEC,
    CompositeCancellation,
    EventCancellation,
    StoreCancellation,
)
from ouroboros.observability.event_record import record_event
from ouroboros.observability.interfaces.audit_events import IAuditEventRepository
from ouroboros.observability.log_context import bind_log_context
from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.runtime_backend import STORE_FAILURES, ExecutionClaimUnavailable, store_deadline
from ouroboros.storage.runtime_models import TaskRecord, stored_task_scope
from ouroboros.storage.task_results import STATUS_FAILED, write_task_result
from ouroboros.storage.tasks.interfaces import ITaskRepository
from ouroboros.storage.tool_confirmation.interfaces import IToolConfirmationRepository
from ouroboros.utils import utc_now_iso
from supervisor.queue import TaskQueue

log = logging.getLogger(__name__)


_FOREGROUND_WORKER_ID = 0
_ACTIVE_TASK_STATUSES = ("queued", "scheduled", "interrupted", "running")

#: Types an earlier version created and this one never runs. Both moved to
#: ``background_memory``, off the task queue entirely. Excluded from the claim,
#: because the foreground worker would otherwise feed a leftover payload — no
#: ``text`` in it — into the agent loop.
_RETIRED_TASK_TYPES = frozenset({"memory_distillation", "context_consolidation"})

#: Rows the A2A server persists to keep protocol state. They live in ``tasks`` because that
#: is the durable store, not because they are queue work: they carry no input and no scope,
#: and a submitted one sits in ``queued`` until the exchange ends.
_PROTOCOL_TASK_TYPES = frozenset({"a2a"})

_UNCLAIMABLE_TASK_TYPES = _RETIRED_TASK_TYPES | _PROTOCOL_TASK_TYPES

# How long a slot waits for a stopped body before giving up on it: above one round's tool
# deadline (OUROBOROS_TOOL_TIMEOUT_SEC, 600s), so a slow tool call still gets to return.
_STOP_GRACE_SECONDS = 900.0

# How often a drain looks at its slots while waiting. Short, because the whole wait is
# short: the drain deadline sits under an orchestrator's termination grace, not above a
# tool deadline like the slot grace above it.
_DRAIN_POLL_INTERVAL_SEC = 0.1
_RELEASE_FAILURES: tuple[type[Exception], ...] = (*STORE_FAILURES, FutureTimeoutError)

#: The lane the single-slot deployment has always used, and the first of several.
_DEFAULT_WORKER_SLOTS = 1


def _configured_worker_slots() -> int:
    """How many runs this instance holds at once. Read here, or the setting controls nothing."""
    from ouroboros.storage.settings.store import load_settings

    try:
        return max(1, int(load_settings().OUROBOROS_WORKER_SLOTS or _DEFAULT_WORKER_SLOTS))
    except Exception:
        log.warning("Unreadable OUROBOROS_WORKER_SLOTS; running one slot", exc_info=True)
        return _DEFAULT_WORKER_SLOTS


@dataclass
class WorkerSlot:
    worker_id: int
    busy_task_id: str | None = None
    claim_token: str = ""
    future: Future | None = None
    #: Stop signal for the task in this slot, replaced by a fresh one on every claim.
    cancellation: EventCancellation = field(default_factory=EventCancellation)
    #: Monotonic time the stop was requested, which starts the grace period.
    stop_requested_at: float | None = None
    #: The composed stop this lane's body observes, kept so the lane can see a stop that
    #: was recorded by another process. The queue that ends a run is a different process in
    #: multi-instance mode and reaches into no lane at all, so this is the only way the
    #: grace clock below ever starts.
    stop_signal: CancellationSignal | None = None

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
        self.claim_token = ""
        self.future = None
        self.cancellation = EventCancellation()
        self.stop_requested_at = None
        self.stop_signal = None


class TaskExecutor:
    """Claims durable tasks from the store and runs them on the foreground worker."""

    def __init__(
        self,
        *,
        repo_dir: pathlib.Path,
        drive_root: pathlib.Path,
        task_queue: TaskQueue,
        tasks_repo: ITaskRepository,
        events: IAgentEventPublisher,
        skill_snapshot: SkillCatalogSnapshot,
        skill_availability: SkillAvailability,
        extension_runtime: IExtensionRuntime,
        session_repository: IOwnerSessionRepository,
        startup_usage_repository: IStartupUsageRepository,
        execution_memory_repository: IExecutionMemoryRepository,
        audit_event_repository: IAuditEventRepository,
        tool_confirmation_reader: IToolConfirmationRepository,
        runtime_id: str,
        work_signal: IWorkSignal,
        outbound: ToolOutbound | None = None,
    ) -> None:
        self.repo_dir = pathlib.Path(repo_dir)
        self.drive_root = pathlib.Path(drive_root)
        self.task_queue = task_queue
        self.tasks_repo = tasks_repo
        #: Who this runtime is on the records it claims. A worker id names a lane inside it.
        self.runtime_id = str(runtime_id)
        #: One direction only: the runs this instance holds report through it and read
        #: nothing back. Whoever consumes those reports holds the other half.
        self._events = events
        self._skill_snapshot = skill_snapshot
        self._skill_availability = skill_availability
        self._extension_runtime = extension_runtime
        self._session_repository = session_repository
        self._startup_usage_repository = startup_usage_repository
        self._execution_memory_repository = execution_memory_repository
        self._audit_event_repository = audit_event_repository
        self._tool_confirmation_reader = tool_confirmation_reader
        #: The outbound boundary every run this instance builds is handed. Injected by the
        #: composition root that owns the gateway; a run never reaches for one.
        self._outbound = outbound
        #: Told when a lane comes free, so a loop asleep on its interval wakes for it: that
        #: owner's next task became claimable at exactly that moment and not before. A hint
        #: like every other on this seam — a lost one costs the interval, not the task.
        self._work_signal = work_signal
        self._pool: ThreadPoolExecutor | None = None
        self._bodies: set[Future] = set()
        self._pool_lock = threading.RLock()
        #: Guards the slot map. The runtime's own, not shared with the queue: after the
        #: split the two components touch nothing in common but the records.
        self._slot_lock = threading.RLock()
        self._slots: dict[int, WorkerSlot] = {}
        self._agent_factory = AgentFactory(
            self.repo_dir,
            self.drive_root,
            self._events,
            self._skill_snapshot,
            self._skill_availability,
            self._extension_runtime,
            self._outbound,
            session_repository=self._session_repository,
            startup_usage_repository=self._startup_usage_repository,
            execution_memory_repository=self._execution_memory_repository,
            tool_confirmation_reader=self._tool_confirmation_reader,
            audit_event_repository=self._audit_event_repository,
        )
        #: Cleared once for good when the drain starts. Claiming has to stop first, or the
        #: instance keeps taking work while trying to give work back and the drain never ends.
        self._claiming = True
        self._claim_lock = threading.Lock()
        for record in self.tasks_repo.list_tasks(statuses=list(_ACTIVE_TASK_STATUSES)):
            if record.task_type in _RETIRED_TASK_TYPES:
                self.tasks_repo.cancel_task(
                    record.task_id,
                    result=f"Task type {record.task_type} is retired and is no longer executed.",
                )

    def new_agent(self) -> OuroborosAgent:
        """One agent for one run; nothing it holds is shared with another slot."""
        return self._agent_factory.create()

    def all_slots(self) -> list[WorkerSlot]:
        return list(self._slots.values())

    def worker_slots_dict(self) -> dict[int, WorkerSlot]:
        return dict(self._slots)

    def get_slot(self, worker_id: int) -> WorkerSlot | None:
        return self._slots.get(worker_id)

    def ensure_started(self) -> None:
        with self._pool_lock:
            if self._pool is None:
                self._agent_factory = AgentFactory(
                    self.repo_dir,
                    self.drive_root,
                    self._events,
                    self._skill_snapshot,
                    self._skill_availability,
                    self._extension_runtime,
                    self._outbound,
                    session_repository=self._session_repository,
                    startup_usage_repository=self._startup_usage_repository,
                    execution_memory_repository=self._execution_memory_repository,
                    tool_confirmation_reader=self._tool_confirmation_reader,
                    audit_event_repository=self._audit_event_repository,
                )
                slots = _configured_worker_slots()
                self._pool = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="ouroboros-task")
                self._slots.clear()
                self._ensure_slots()
                log.info(
                    "Supervisor task executor pool started slots=%d drive_root=%s repo_dir=%s",
                    slots,
                    self.drive_root,
                    self.repo_dir,
                )
                record_event(
                    "supervisor",
                    "single_process_executor_start",
                    {"ts": utc_now_iso(), "count": slots},
                    audit_event_repository=self._audit_event_repository,
                )

    def _ensure_slots(self) -> None:
        """Open the configured lanes, without disturbing one that already holds a run."""
        for worker_id in range(_configured_worker_slots()):
            self._slots.setdefault(worker_id, WorkerSlot(worker_id=worker_id))

    @contextmanager
    def _claim_admission(self, slot: WorkerSlot, record: TaskRecord) -> Iterator[bool]:
        with self._claim_lock:
            yield self._claiming
            if self._claiming:
                slot.busy_task_id = record.task_id
                slot.claim_token = str(record.metadata.get("execution_claim") or "")
                slot.cancellation = EventCancellation()
                slot.stop_requested_at = None

    def begin_drain(self) -> None:
        with self._claim_lock:
            self._claiming = False

    def held_task_ids(self) -> list[str]:
        with self._slot_lock:
            return [slot.busy_task_id for slot in self._slots.values() if slot.busy_task_id]

    def handle_stop_notice(self, notice: TaskStopNotice) -> None:
        """Set local signals for tasks this runtime currently holds."""
        if notice.runtime_id != self.runtime_id:
            return
        wanted = frozenset(notice.task_ids)
        with self._slot_lock:
            for slot in self._slots.values():
                if slot.busy_task_id in wanted:
                    slot.request_stop()

    def drain(self, *, deadline_sec: float) -> bool:
        deadline = time.monotonic() + max(0.0, deadline_sec)
        self.begin_drain()
        grace_end = deadline - min(1.0, max(0.0, deadline_sec) / 5)
        while time.monotonic() < grace_end:
            if not self._slot_lock.acquire(timeout=max(0.0, grace_end - time.monotonic())):
                break
            try:
                if all(
                    (slot.future is not None and slot.future.done()) or not slot.busy_task_id
                    for slot in self._slots.values()
                ):
                    break
            finally:
                self._slot_lock.release()
            time.sleep(min(_DRAIN_POLL_INTERVAL_SEC, max(0.0, grace_end - time.monotonic())))
        self.shutdown(deadline=deadline)
        with self._claim_lock:
            bodies = tuple(self._bodies)
        return not wait(bodies, timeout=max(0.0, deadline - time.monotonic())).not_done

    def shutdown(self, *, deadline: float | None = None) -> None:
        deadline = time.monotonic() + 5 if deadline is None else deadline
        self.begin_drain()
        claims = {s.busy_task_id: s.claim_token for s in list(self._slots.values()) if s.busy_task_id}
        token = store_deadline.set(deadline)
        release_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="runtime-release")
        try:
            release = release_pool.submit(
                contextvars.copy_context().run,
                self.task_queue.release_own_runs,
                runtime_id=self.runtime_id,
                claims=claims,
            )
            release.add_done_callback(
                lambda future: (
                    log.warning("Runtime release deferred: %s", future.exception())
                    if not future.cancelled() and future.exception() is not None
                    else None
                )
            )
            try:
                release.result(timeout=max(0.0, deadline - time.monotonic()))
            except _RELEASE_FAILURES:
                release.cancel()
                log.warning("Runtime release exhausted its grace budget; lease recovery retains ownership")
        finally:
            store_deadline.reset(token)
            release_pool.shutdown(wait=False, cancel_futures=True)
            # Local signals also fence a body when an unavailable database defers release.
            for slot in list(self._slots.values()):
                slot.request_stop()
            if self._slot_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                try:
                    for slot in self._slots.values():
                        slot.clear_task()
                finally:
                    self._slot_lock.release()
            if self._pool_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                try:
                    if self._pool is not None:
                        self._pool.shutdown(wait=False, cancel_futures=True)
                    self._pool = None
                finally:
                    self._pool_lock.release()

    def assign_next(self) -> None:
        """Fill every free slot, not only the first: a slot left idle is a run not started.

        The claim decides what each may take — including that one owner holds one root run
        — so the loop here only offers each free slot its turn.
        """
        if not self._claiming:
            return
        with self._slot_lock:
            if not self._claiming:
                return
            self.ensure_started()
            self._ensure_slots()
            for slot in sorted(self._slots.values(), key=lambda item: item.worker_id):
                # A ``None`` means either the slot is busy or nothing was claimable. Only the
                # second ends the sweep: a busy slot must not stop the ones behind it.
                started = self._assign_slot(slot, exclude_task_types=_UNCLAIMABLE_TASK_TYPES)
                if started is None and slot.busy_task_id is None:
                    break

    def ensure_healthy(self) -> int:
        """Reap finished futures and free abandoned lanes; report how many came free.

        The count is what lets the loop re-check for work immediately instead of waiting
        out its interval: that owner's next task became claimable at exactly that moment,
        because the claim's one-run-per-owner predicate was hiding it until then.
        """
        freed = 0
        with self._slot_lock:
            for slot in self._slots.values():
                if slot.future is None:
                    continue
                if slot.future.done():
                    self._finish_slot_future(slot)
                    freed += 1
                    continue
                self._observe_recorded_stop(slot)
                if self._release_if_abandoned(slot):
                    freed += 1
        return freed

    def _observe_recorded_stop(self, slot: WorkerSlot) -> None:
        """Start the grace clock from the stop the *record* carries.

        Nothing pokes a lane any more: the queue that hard-times-out a run and the command
        that cancels one both live in the supervisor tier, which is another process. The
        lane learns the way its own body does — through the signal it handed out — and that
        read is what arms the grace period. Without it the clock is never set outside a
        drain, so `_release_if_abandoned` below can never fire and a body that ignores its
        stop holds this lane for the life of the process.
        """
        if slot.stop_requested_at is not None or slot.busy_task_id is None or slot.stop_signal is None:
            return
        try:
            stopped = slot.stop_signal.is_stopped()
        except (Exception, ExecutionClaimUnavailable):
            log.debug("Failed to read the stop state of task %s", slot.busy_task_id, exc_info=True)
            return
        if stopped:
            slot.stop_requested_at = time.monotonic()

    def _replace_pool(self) -> None:
        """Give the remaining slots their threads back, since the abandoned one keeps one.

        The leaked thread belongs to the pool for as long as it lives, so without this every
        abandonment costs the instance a worker permanently. The old pool is shut down
        without waiting, so its queued items still run and its idle threads exit.
        """
        with self._pool_lock:
            old_pool = self._pool
            if old_pool is None:
                return
            self._pool = ThreadPoolExecutor(max_workers=_configured_worker_slots(), thread_name_prefix="ouroboros-task")
            old_pool.shutdown(wait=False)

    def _release_if_abandoned(self, slot: WorkerSlot) -> bool:
        """Free a slot whose body has not honoured its stop signal within the grace period.

        A ``task_abandoned`` event is a real leak: the thread is still running.
        """
        requested_at = slot.stop_requested_at
        if requested_at is None or slot.busy_task_id is None:
            return False
        elapsed = time.monotonic() - requested_at
        if elapsed < _STOP_GRACE_SECONDS:
            return False
        task_id = slot.busy_task_id
        log.warning(
            "Task %s did not stop %ds after the stop signal; releasing worker %d",
            task_id,
            int(elapsed),
            slot.worker_id,
        )
        record_event(
            "supervisor",
            "task_abandoned",
            {
                "ts": utc_now_iso(),
                "task_id": task_id,
                "worker_id": slot.worker_id,
                "elapsed_sec": round(elapsed, 2),
                "grace_sec": _STOP_GRACE_SECONDS,
            },
            audit_event_repository=self._audit_event_repository,
        )
        # Nothing renews this record's lease on purpose any more — except the leaked body's
        # own heartbeat thread, which only `handle_task`'s `finally` stops and which the
        # abandoned body never reaches. Left running, that beat keeps the row too fresh for
        # the stale-lease sweep to ever select, so the task stays `running` forever and
        # holds its owner's one root slot. Closing it here is what stops the beat mattering:
        # a heartbeat only records liveness for a record that is still running.
        self._write_failure_result(
            task_id,
            reason=f"Task abandoned: its body did not stop {int(elapsed)}s after the stop signal.",
        )
        # The future is left alone: the thread cannot be killed and reading its result here
        # would block the tick, so dropping the handle is all the slot can do. Nothing else
        # is replaced: the leaked body holds its own agent and its own signal, so the next
        # task in this slot starts on a runtime the leak cannot reach.
        self._replace_pool()
        slot.clear_task()
        return True

    def _assign_slot(
        self,
        slot: WorkerSlot,
        *,
        include_task_types: frozenset[str] | None = None,
        exclude_task_types: frozenset[str] | None = None,
    ) -> TaskRecord | None:
        """Claim and start the next matching task; return its record once started."""
        if not self._claiming:
            return None
        if slot.busy_task_id is not None:
            if slot.future is not None and slot.future.done():
                self._finish_slot_future(slot)
            else:
                return None
        record = self.task_queue.claim_next(
            worker_id=slot.worker_id,
            runtime_id=self.runtime_id,
            include_task_types=include_task_types,
            exclude_task_types=exclude_task_types,
            claim_guard=lambda record: self._claim_admission(slot, record),
        )
        if record is None:
            return None
        with self._claim_lock:
            if not self._claiming or self._pool is None:
                if slot.cancellation.is_stopped():
                    slot.clear_task()
                return None
            stop_signal = self._stop_signal_for(record, slot)
            slot.stop_signal = stop_signal
            slot.future = future = self._pool.submit(self._run_task, record, stop_signal)
            self._bodies.add(future)
        future.add_done_callback(self._announce_free_lane)
        return record

    def _announce_free_lane(self, _future: Future) -> None:
        """A run has returned, so the claim may have something for its owner again."""
        with self._claim_lock:
            self._bodies.discard(_future)
        try:
            if not _future.cancelled() and _future.exception() is not None:
                log.warning("Task body future failed: %s", _future.exception())
            self._work_signal.signal()
        except Exception:
            log.debug("Failed to signal that a lane came free", exc_info=True)

    def _stop_signal_for(self, record: TaskRecord, slot: WorkerSlot) -> CancellationSignal:
        """The stop this body observes: the slot's event, and the record behind it.

        Composed rather than replaced. A stop asked for here is instant; one recorded by
        another instance arrives within the poll interval. A record that names no
        conversation gets the event alone — there is no owner to scope a stop write to,
        and inventing one would let a stop land on somebody else's run.
        """
        scope = stored_task_scope(record.to_agent_input().get("scope"))
        if scope is None:
            log.warning("Task %s names no scope; its stop signal cannot cross instances", record.task_id)
            return slot.cancellation
        return CompositeCancellation(
            slot.cancellation,
            StoreCancellation(
                self.tasks_repo,
                task_id=record.task_id,
                scope=scope,
                poll_interval_sec=STOP_POLL_INTERVAL_SEC,
                claim_token=str(record.metadata.get("execution_claim") or "") if record.detached else None,
            ),
        )

    def _run_task(self, record: Any, cancellation: CancellationSignal) -> None:
        task = record.to_agent_input()
        try:
            task_id = str(record.task_id or "")
            correlation_id = str(task.get("correlation_id") or task_id or "")
            with bind_log_context(task_id=task_id, correlation_id=correlation_id):
                # Built here, for this run only: two runs never meet inside one agent, which
                # is what a lock around this line used to be standing in for.
                agent = self.new_agent()
                reports = agent.handle_task(task, cancellation=cancellation)
            for report in reports:
                self._publish(report, task_id)
        except ExecutionClaimUnavailable:
            log.warning("Task %s deferred to lease recovery after store failure", record.task_id, exc_info=True)
        except Exception as exc:
            task_id = str(record.task_id or "")
            worker_id = int(record.worker_id if record.worker_id is not None else _FOREGROUND_WORKER_ID)
            scope = stored_task_scope(task.get("scope"))
            self._log_worker_crash(worker_id, "handle_task", exc, "")
            if record.detached:
                try:
                    closed = self.tasks_repo.finish_detached_attempt(
                        task_id,
                        status="failed",
                        result="Task failed in the agent runtime.",
                        error_code="execution_failed",
                        claim_token=str(record.metadata.get("execution_claim") or ""),
                    )
                except STORE_FAILURES:
                    log.warning("Task %s deferred to lease recovery after store failure", task_id, exc_info=True)
                    return
                if closed is not None and scope is not None:
                    self._publish(
                        TaskFinished(
                            scope=scope,
                            task_id=task_id,
                            task_type=record.task_type,
                            status=closed.status,
                            result=closed.result,
                            ts=closed.finished_at,
                        ),
                        task_id,
                    )
                return
            # The record is closed here, by the runtime that held the run: a crash is still
            # an outcome this component observed, and nothing downstream is required for it.
            self._write_failure_result(
                task_id,
                scope=scope,
                reason=f"Task failed in the agent runtime: {type(exc).__name__}: {exc}",
            )
            if scope is not None:
                self._publish(
                    TaskFinished(
                        scope=scope,
                        task_id=task_id,
                        task_type=str(record.task_type or ""),
                        status=STATUS_FAILED,
                        ts=utc_now_iso(),
                    ),
                    task_id,
                )

    def _publish(self, report: AgentEvent, task_id: str) -> None:
        """Report one fact about a run, and never let the reporting decide the run.

        Its own guard, as every publish on this seam already has
        (`ouroboros/agent/core.py`). In-process this is unreachable — `queue.put` blocks and
        does not raise — but on a broker a connection blip inside the loop above would reach
        the handler that writes `STATUS_FAILED` over a record the run already closed
        successfully: a fact recorded by a component that did not witness it, which is the
        precise thing this seam exists to prevent.

        The report is lost, and how much that costs depends on the kind, so the level does
        too. A droppable kind is a display update the transport regenerates. Anything else
        is the message the owner is waiting for: the answer is still on the task record, so
        the task is intact and the conversation is not, and that is an operator's problem
        rather than a debug line. Reporting is not retried here — the append deliberately
        has no retry, because a repeated one publishes the answer twice.
        """
        try:
            self._events.publish(report)
        except Exception:
            if type(report).droppable:
                log.warning("Failed to report a %s for task %s", type(report).__name__, task_id, exc_info=True)
                return
            log.error(
                "Lost a %s for task %s: the transport refused it, so the owner will not see this "
                "message. The task's own record still holds its outcome.",
                type(report).__name__,
                task_id,
                exc_info=True,
            )

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
        *,
        scope: OwnerScope | None = None,
        reason: str = "Task was not completed.",
        status: str = "",
    ) -> None:
        """Close a task that crashed. ``scope`` is only needed if the row does not exist yet."""
        if not task_id:
            return
        try:
            write_task_result(
                self.drive_root,
                task_id,
                status or STATUS_FAILED,
                scope=scope,
                result=reason,
                total_rounds=0,
            )
        except Exception:
            log.warning("Failed to write failure result for task %s", task_id, exc_info=True)

    def _log_worker_crash(self, wid: int, phase: str, exc: Exception, tb: str) -> None:
        try:
            record_event(
                "supervisor",
                "executor_task_error",
                {
                    "ts": utc_now_iso(),
                    "worker_id": wid,
                    "phase": phase,
                    "error": repr(exc),
                    "traceback": str(tb)[:3000],
                },
                audit_event_repository=self._audit_event_repository,
            )
        except Exception:
            log.debug("Suppressed executor crash log failure", exc_info=True)


__all__ = [
    "TaskExecutor",
    "WorkerSlot",
]
