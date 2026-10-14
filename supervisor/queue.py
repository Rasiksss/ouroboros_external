"""Supervisor task queue backed by the runtime metadata store."""

from __future__ import annotations

import datetime
import logging
import pathlib
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from new_core.coordination.inprocess import NullWorkSignal
from new_core.coordination.interfaces import IWorkSignal
from new_core.identity import OwnerScope
from ouroboros.observability.event_record import record_event
from ouroboros.observability.interfaces.audit_events import IAuditEventRepository
from ouroboros.storage.runtime_models import ACTIVE_STATUSES, TaskRecord, TaskRequest, stored_task_scope
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.schema import SettingsLike
from ouroboros.storage.task_results import HEARTBEAT_STALE_SEC, TERMINAL_STATUSES
from ouroboros.storage.tasks.interfaces import ITaskRepository
from ouroboros.storage.tool_confirmation.interfaces import IToolConfirmationRepository
from ouroboros.utils import utc_now_iso
from supervisor.message_bus import send_with_budget

log = logging.getLogger(__name__)

QUEUE_MAX_RETRIES: int = 1

#: The three ways a running record is ended from outside, kept apart on the record itself.
#: One says the task took too long, one says the process running it stopped reporting, and
#: one says the process running it left on purpose. Folding them together would make a lost
#: pod look like a slow prompt, and an ordinary deploy look like either.
HARD_TIMEOUT_ERROR_CODE = "hard_timeout"
LEASE_LOST_ERROR_CODE = "lease_lost"
RELEASE_ERROR_CODE = "instance_released"

_queue_lock = threading.RLock()


def _stopped_notice(task_id: str) -> str:
    return f"🛑 Task {task_id} stopped as requested."


def _retry_notice(requeued: bool, attempt: int) -> str:
    return f"Task queued for retry attempt={attempt}." if requeued else "Retry limit exhausted, task stopped."


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


class TaskQueue:
    """Durable task queue over the runtime metadata store."""

    def __init__(
        self,
        tasks_repo: ITaskRepository,
        drive_root: pathlib.Path,
        *,
        work_signal: IWorkSignal,
        audit_event_repository: IAuditEventRepository,
        soft_timeout: int = 600,
        hard_timeout: int = 1800,
        max_running_root_jobs_per_owner: int = 3,
    ) -> None:
        self.tasks_repo = tasks_repo
        self.audit_event_repository = audit_event_repository
        self.drive_root = pathlib.Path(drive_root)
        #: Told whenever a task becomes claimable, so a runtime need not wait out its
        #: interval. An optimisation and never a guarantee: a runtime polls the claim on
        #: its own, and a signal nobody delivers costs latency rather than a stranded task.
        self._work_signal = work_signal
        self.soft_timeout_sec = int(soft_timeout)
        self.hard_timeout_sec = int(hard_timeout)
        self.max_running_root_jobs_per_owner = int(max_running_root_jobs_per_owner)

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
        pending = self.tasks_repo.list_tasks(statuses=["queued", "scheduled", "interrupted"])
        if not pending:
            return base - 1000
        return min(int(record.priority) for record in pending) - 1000

    def enqueue(self, request: TaskRequest, *, front: bool = False) -> TaskRecord:
        payload = request.model_copy(deep=True)
        if not payload.id:
            payload.id = uuid.uuid4().hex[:8]
        with _queue_lock:
            existing = self.tasks_repo.get_task(payload.id)
            if existing is not None and existing.status != "requested":
                return existing
            base_priority = int(payload.priority) if payload.priority else _default_task_priority(payload.type)
            payload.priority = self._allocate_priority(base_priority, front=front)
            if not payload.queued_at:
                payload.queued_at = utc_now_iso()
            if existing is None:
                created = self.tasks_repo.create_task(payload, status="queued")
                self.announce_claimable()
                return created
            requeued_scope = stored_task_scope(payload.scope)
            scheduled = existing.model_copy(
                update={
                    # The ownership columns travel with the update, or the row keeps whoever
                    # created it while ``input["scope"]`` says someone else — a disagreement
                    # no statement would fail on.
                    **(
                        {"owner_id": requeued_scope.owner_id, "agent_id": requeued_scope.agent_id}
                        if requeued_scope is not None
                        else {}
                    ),
                    "source": payload.source,
                    "correlation_id": payload.correlation_id,
                    "status": "scheduled",
                    "task_type": payload.type,
                    "chat_id": payload.chat_id,
                    "input": payload.to_task_dict(),
                    "parent_task_id": payload.parent_task_id,
                    "result": "",
                    "priority": payload.priority,
                    "attempt": max(1, int(payload.attempt or 1)),
                    "queued_at": payload.queued_at,
                }
            )
            queued = self.tasks_repo.upsert_task(scheduled, expected_status="requested")
        self.announce_claimable()
        return queued

    def announce_claimable(self) -> None:
        """Say a task may be claimable now. A failed hint must never fail an enqueue."""
        try:
            self._work_signal.signal()
        except Exception:
            log.debug("Failed to signal that a task became claimable", exc_info=True)

    def enqueue_dict(self, task: dict[str, Any], *, front: bool = False) -> TaskRecord:
        return self.enqueue(TaskRequest.model_validate(task), front=front)

    def claim_next(
        self,
        worker_id: int = 0,
        *,
        runtime_id: str,
        include_task_types: Iterable[str] | None = None,
        exclude_task_types: Iterable[str] | None = None,
        claim_guard: Callable[[TaskRecord], AbstractContextManager[bool]] | None = None,
    ) -> TaskRecord | None:
        """Take the next claimable task, stamping the caller's identity on the record.

        The identity is the claimer's argument rather than this queue's field: whoever
        runs the task is who the record has to name, and the queue does not run tasks.
        """
        return self.tasks_repo.claim_next_task(
            worker_id=worker_id,
            runtime_id=str(runtime_id),
            include_task_types=include_task_types,
            exclude_task_types=exclude_task_types,
            max_running_root_jobs_per_owner=self.max_running_root_jobs_per_owner,
            claim_guard=claim_guard,
        )

    def queue_has_task_type(self, task_type: str) -> bool:
        wanted = str(task_type or "")
        for record in self.tasks_repo.list_tasks(statuses=list(ACTIVE_STATUSES)):
            if str(record.task_type or "") == wanted:
                return True
        return False

    def list_pending_records(self, *, scope: OwnerScope | None = None) -> list[TaskRecord]:
        return self.tasks_repo.list_tasks(statuses=["queued", "scheduled", "interrupted"], scope=scope)

    def list_running(self, *, scope: OwnerScope | None = None) -> dict[str, RunningTaskMeta]:
        """Every running record, or one owner's when a scope narrows it.

        The unscoped read is the sweep's and the shutdown's — they act on the process, not
        for anybody. Anything answering an owner passes their scope.
        """
        running: dict[str, RunningTaskMeta] = {}
        for record in self.tasks_repo.list_tasks(statuses=["running"], scope=scope, limit=None):
            ran_from = str(record.metadata.get("run_started_at") or "")
            started_ts = (
                parse_iso_to_ts(ran_from) or (0.0 if record.detached else parse_iso_to_ts(record.started_at)) or 0.0
            )
            hb_ts = parse_iso_to_ts(record.heartbeat_at) or started_ts
            running[record.task_id] = RunningTaskMeta(
                record=record,
                worker_id=int(record.worker_id or 0),
                started_at=started_ts,
                last_heartbeat_at=hb_ts,
                soft_sent=bool(record.soft_notice_at),
                attempt=int(record.attempt or 1),
            )
        return running

    def drain_all_pending(self) -> list[TaskRecord]:
        drained: list[TaskRecord] = []
        for record in self.list_pending_records():
            drained.append(record)
            self.tasks_repo.fail_task(record.task_id, result="Task drained during supervisor shutdown.")
        return drained

    def cancel_task_by_id(self, task_id: str, *, scope: OwnerScope, job_id: str = "") -> bool:
        """Cancel one of the caller's own tasks, addressed by its record.

        The record decides everything: that the task exists, that it belongs to the caller,
        and which instance holds it. Closing it is the whole of the cancellation — a running
        body observes its own record through ``StoreCancellation`` and stops within one poll
        interval, wherever that body happens to be running.
        """
        with _queue_lock:
            if job_id:
                return bool(self.tasks_repo.stop_jobs(scope=scope, job_id=job_id))
            record = self.tasks_repo.get_task(str(task_id))
            if record is None or record.status in TERMINAL_STATUSES:
                return False
            if (record.owner_id, record.agent_id) != (scope.owner_id, scope.agent_id):
                return False
            if record.status != "running":
                self.tasks_repo.cancel_task(task_id, result="Task cancelled by user/agent request.")
                return True
            # Terminal first: a body returning in this window has to find its task closed.
            # Every run observes exactly this through its own signal, here or elsewhere.
            self.tasks_repo.cancel_task(task_id, result="Running task cancelled.")
            return True

    def _notice_route(self, record: TaskRecord) -> tuple[OwnerScope | None, str, dict[str, Any]]:
        """Where a notice about this record goes: scope, delivery source, transport hints.

        The record's own scope and nothing else. The instance's stored owner used to stand
        in when a record named none, which was defensible only while every record belonged
        to the same person: with real owners it addresses one owner's notice to whoever
        happened to be in the process row. A record that names no scope is not announced.
        """
        if record.detached:
            return None, "unknown", {}
        task_input = record.to_agent_input()
        scope = stored_task_scope(task_input.get("scope"))
        source = str(record.source or "").strip()
        transport = task_input.get("transport")
        return scope, source or "unknown", dict(transport) if isinstance(transport, dict) else {}

    def _retry_request(
        self,
        task_input: dict[str, Any],
        *,
        task_id: str,
        new_attempt: int,
        marker: str,
    ) -> TaskRequest | None:
        """Build the clone inserted atomically with predecessor closure."""
        retried = dict(task_input)
        retried["original_task_id"] = task_id
        retried["id"] = uuid.uuid5(uuid.NAMESPACE_URL, f"task-successor:{task_id}").hex
        retried["_attempt"] = new_attempt
        retried[f"{marker}_from"] = task_id
        retried[f"{marker}_at"] = utc_now_iso()
        try:
            if stored_task_scope(retried.get("scope")) is None:
                raise ValueError("retry has no owner scope")
            request = TaskRequest.model_validate(retried)
            request.priority = self._allocate_priority(int(request.priority or 1), front=True)
            request.queued_at = utc_now_iso()
        except ValueError:
            # A stored input that names no scope cannot be queued again. The record is
            # terminal either way, and the sweep still owes the other records their turn.
            log.warning("Requeue of task %s (%s) could not be queued", task_id, marker, exc_info=True)
            return None
        return request

    def reconcile_parked_tasks(self, confirmations: IToolConfirmationRepository) -> list[str]:
        """Hold the two halves of one invariant: a question is open exactly while its run is parked."""
        for closed in confirmations.close_confirmations_nobody_is_parked_on(now_iso=utc_now_iso()):
            log.info(
                "event=tool_confirmation_expired task_id=%s tool=%s confirmation_id=%s",
                closed.task_id,
                closed.tool_name,
                closed.confirmation_id,
            )
        confirmations.forget_snapshots_of_finished_runs()
        woken = [
            task_id
            for task_id in confirmations.list_parked_tasks_without_pending_request()
            if confirmations.requeue_parked_task(task_id)
        ]
        if woken:
            self.announce_claimable()
        return woken

    def reclaim_stale_leases(self) -> list[TaskRecord]:
        cutoff = _iso_seconds_ago(HEARTBEAT_STALE_SEC)
        return [
            closed
            for task in self.tasks_repo.list_stale_leases(cutoff_iso=cutoff)
            if (closed := self._recover_running(task, LEASE_LOST_ERROR_CODE, cutoff=cutoff)) is not None
        ]

    def release_own_runs(self, *, runtime_id: str, claims: dict[str, str] | None = None) -> list[TaskRecord]:
        released = []
        for task in self.tasks_repo.list_tasks(statuses=["running"], runtime_id=runtime_id, limit=None):
            if (
                claims is not None
                and task.detached
                and claims.get(task.task_id) != task.metadata.get("execution_claim")
            ):
                continue
            if (closed := self._recover_running(task, RELEASE_ERROR_CODE)) is not None:
                released.append(closed)
        return released

    def _recover_running(
        self, task: TaskRecord, reason: str, *, cutoff: str = "", runtime_sec: float = 0
    ) -> TaskRecord | None:
        released = reason == RELEASE_ERROR_CODE
        attempt = int(task.attempt or 1)
        retry = not task.stop_requested_at and (released or attempt <= QUEUE_MAX_RETRIES)
        new_attempt = attempt + int(retry and not released)
        marker, event_name, detail = {
            RELEASE_ERROR_CODE: (
                "instance_release",
                "task_released",
                "Task released: the instance running it shut down, and the work was handed back.",
            ),
            LEASE_LOST_ERROR_CODE: (
                "lease_reclaim",
                "task_lease_reclaimed",
                f"Task reclaimed: the instance holding it stopped reporting for more than {HEARTBEAT_STALE_SEC}s.",
            ),
            HARD_TIMEOUT_ERROR_CODE: (
                "timeout_retry",
                "task_hard_timeout",
                f"Task killed by hard timeout after {int(runtime_sec)}s.",
            ),
        }[reason]
        successor = (
            self._retry_request(task.to_agent_input(), task_id=task.task_id, new_attempt=new_attempt, marker=marker)
            if retry
            else None
        )
        transition = self.tasks_repo.transition_running(
            task.task_id,
            kind=reason,
            runtime_id=str(task.runtime_id or ""),
            claim_token=str(task.metadata.get("execution_claim") or ""),
            cutoff_iso=cutoff,
            result=detail,
            attempt=new_attempt,
            successor=successor,
            hard_timeout_sec=self.hard_timeout_sec,
        )
        if transition is None:
            return None
        record, requeued = transition
        if requeued:
            self.announce_claimable()
        record_event(
            "supervisor",
            event_name,
            {
                "ts": utc_now_iso(),
                "task_id": record.task_id,
                "task_type": record.task_type,
                "runtime_id": record.runtime_id,
                "worker_id": int(record.worker_id or 0),
                "heartbeat_at": task.heartbeat_at,
                "stale_after_sec": HEARTBEAT_STALE_SEC,
                "runtime_sec": round(runtime_sec, 2),
                "attempt": attempt,
                "requeued": requeued,
                "new_attempt": new_attempt,
                "max_retries": QUEUE_MAX_RETRIES,
            },
            audit_event_repository=self.audit_event_repository,
        )
        scope, source, transport = self._notice_route(record)
        if scope is not None:
            notice = {
                RELEASE_ERROR_CODE: f"♻️ Task {record.task_id} was handed back: the instance running it is shutting down. Task queued to continue on another instance.",
                LEASE_LOST_ERROR_CODE: f"🔌 Task {record.task_id} was running on an instance that stopped reporting. {_retry_notice(requeued, new_attempt)}",
                HARD_TIMEOUT_ERROR_CODE: f"🛑 Hard-timeout: task {record.task_id} killed after {int(runtime_sec)}s.\n{_retry_notice(requeued, new_attempt)}",
            }[reason]
            self._notify_owner(
                scope=scope,
                text=_stopped_notice(record.task_id) if record.stop_requested_at else notice,
                source=source,
                transport=transport,
                task_id=record.task_id,
            )
        return record

    def enforce_task_timeouts(self) -> None:
        running = self.list_running()
        if not running:
            return

        now = time.time()

        for task_id, meta in list(running.items()):
            started_at = float(meta.started_at or 0.0)
            if started_at <= 0:
                continue
            last_hb = float(meta.last_heartbeat_at or started_at)
            runtime_sec = max(0.0, now - started_at)
            hb_lag_sec = max(0.0, now - last_hb)
            task_type = str(meta.record.task_type or "")
            notice_scope, route_source, route_transport = self._notice_route(meta.record)

            # The claim decides and the listing is only a hint: every instance sweeping this
            # record listed it before any of them wrote, so all of them read the flag unset.
            # The claim applies for exactly one of them, and for none once the record has
            # stopped running — a task that finished is not announced as still going. The
            # hint only spares an already-noticed record a statement, and a record nobody can
            # be told about is not claimed at all: the claim records that a notice went out.
            unnoticed = runtime_sec >= self.soft_timeout_sec and not meta.soft_sent
            if notice_scope is not None and unnoticed and self.tasks_repo.claim_soft_timeout_notice(task_id):
                self._notify_owner(
                    scope=notice_scope,
                    text=(
                        f"⏱️ Task {task_id} running for {int(runtime_sec)}s. "
                        f"type={task_type}, heartbeat_lag={int(hb_lag_sec)}s. Continuing."
                    ),
                    source=route_source,
                    transport=route_transport,
                    task_id=task_id,
                    is_progress=True,
                )

            if runtime_sec < self.hard_timeout_sec:
                continue

            self._recover_running(meta.record, HARD_TIMEOUT_ERROR_CODE, runtime_sec=runtime_sec)

    def _notify_owner(
        self,
        *,
        scope: OwnerScope,
        text: str,
        source: str,
        transport: dict[str, Any],
        task_id: str,
        is_progress: bool = False,
    ) -> None:
        """Deliver a task notice without letting transport failure stop the sweep."""
        if source == "unknown":
            # No transport claims it, so the notice is about to vanish; say so once.
            log.warning(
                "Task notice for task %s in chat %s has no deliverable source",
                task_id,
                scope.chat_id,
            )
        try:
            # The outbox is the fanout: the notice goes out once and whichever transport
            # serves `source` delivers it.
            send_with_budget(text, scope=scope, source=source, transport=transport, is_progress=is_progress)
        except Exception:
            log.warning("Failed to deliver a task notice to chat %s", scope.chat_id, exc_info=True)


def _iso_seconds_ago(seconds: float) -> str:
    """The ISO instant ``seconds`` in the past, in the format the store's columns hold.

    The comparison is textual, which is exact for these timestamps: every one is written
    by ``utc_now_iso`` and so shares a format and a zone.
    """
    return (datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=seconds)).isoformat()


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


def get_task_queue(
    drive_root: pathlib.Path | None = None, *, audit_event_repository: IAuditEventRepository
) -> TaskQueue:
    if drive_root is not None:
        root = pathlib.Path(drive_root)
        tasks_repo = runtime_persistence.current.tasks_repo
        from supervisor.runtime_context import try_get_runtime

        runtime = try_get_runtime()
        soft = runtime.soft_timeout if runtime is not None else 600
        hard = runtime.hard_timeout if runtime is not None else 1800
        # Reads only: this handle answers counts for a status endpoint and never enqueues,
        # so it has nobody to wake.
        return TaskQueue(
            tasks_repo,
            root,
            work_signal=NullWorkSignal(),
            soft_timeout=soft,
            hard_timeout=hard,
            audit_event_repository=audit_event_repository,
        )
    return _runtime_queue()


def init(
    drive_root: pathlib.Path, soft_timeout: int, hard_timeout: int, *, audit_event_repository: IAuditEventRepository
) -> TaskQueue:
    """Create a task queue for tests; production uses build_supervisor_runtime."""
    root = pathlib.Path(drive_root)
    runtime_persistence.initialize()
    tasks_repo = runtime_persistence.current.tasks_repo
    return TaskQueue(
        tasks_repo,
        root,
        work_signal=NullWorkSignal(),
        soft_timeout=soft_timeout,
        hard_timeout=hard_timeout,
        audit_event_repository=audit_event_repository,
    )


def refresh_timeouts_from_settings(settings: SettingsLike) -> None:
    _runtime_queue().refresh_timeouts_from_settings(settings)


def enqueue_task(task: dict[str, Any], front: bool = False) -> TaskRecord:
    return _runtime_queue().enqueue_dict(task, front=front)


def queue_has_task_type(task_type: str) -> bool:
    return _runtime_queue().queue_has_task_type(task_type)


def drain_all_pending() -> list[dict[str, Any]]:
    return [dict(record.input) for record in _runtime_queue().drain_all_pending()]


def cancel_task_by_id(task_id: str, scope: OwnerScope, *, job_id: str = "") -> bool:
    return _runtime_queue().cancel_task_by_id(task_id, scope=scope, job_id=job_id)


def enforce_task_timeouts() -> None:
    _runtime_queue().enforce_task_timeouts()


def reclaim_stale_leases() -> list[TaskRecord]:
    return _runtime_queue().reclaim_stale_leases()


__all__ = [
    "HARD_TIMEOUT_ERROR_CODE",
    "HEARTBEAT_STALE_SEC",
    "LEASE_LOST_ERROR_CODE",
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
    "parse_iso_to_ts",
    "queue_has_task_type",
    "reclaim_stale_leases",
    "refresh_timeouts_from_settings",
]
