"""Task status helpers backed by the runtime metadata store."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, Literal, TypedDict

from new_core.identity import OwnerScope
from ouroboros.storage.runtime_backend import STORE_FAILURES, ExecutionClaimUnavailable
from ouroboros.storage.runtime_models import TaskRecord, normalize_task_source
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.utils import utc_now_iso

#: How often a running body renews its lease. Named here rather than left a literal inside
#: the loop, because the stale interval the sweep reads is derived from it: two constants
#: that must agree, and can be tuned separately, will eventually disagree.
HEARTBEAT_PERIOD_SEC: int = 30
HEARTBEAT_STALE_SEC = 4 * HEARTBEAT_PERIOD_SEC
execution_claim: ContextVar[tuple[str, str, Callable[[], bool]] | None] = ContextVar("execution_claim", default=None)


class ExecutionClaimLost(RuntimeError):
    """The task no longer has authority to execute under its current claim."""


def check_execution_claim(*, model_start: bool = False) -> None:
    claim = execution_claim.get()
    if claim is None:
        return
    task_id, token, stopped = claim
    store = runtime_persistence.current.tasks_repo
    try:
        if stopped() or not store.is_task_attempt_active(task_id, claim_token=token):
            raise ExecutionClaimLost("Task execution authority lost")
        if model_start and store.heartbeat_task(task_id, phase="model", claim_token=token) is None:
            raise ExecutionClaimLost("Task execution authority lost before model dispatch")
    except STORE_FAILURES as exc:
        raise ExecutionClaimUnavailable(task_id) from exc


STATUS_REQUESTED = "requested"
STATUS_SCHEDULED = "scheduled"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_REJECTED_DUPLICATE = "rejected_duplicate"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUS_CANCELLED = "cancelled"
STATUS_BUDGET_EXCEEDED = "budget_exceeded"

TERMINAL_STATUSES = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_REJECTED_DUPLICATE,
        STATUS_CANCELLED,
        STATUS_BUDGET_EXCEEDED,
    }
)

PublicJobStatus = Literal["queued", "running", "stopping", "succeeded", "failed", "cancelled"]


class JobSummary(TypedDict):
    surface_id: str
    chat_id: str
    detached: bool
    job_id: str
    status: PublicJobStatus
    created_at: str
    started_at: str | None
    preview: str


class JobDetail(JobSummary):
    task: str
    finished_at: str | None
    result: str | None
    error: str | None


def public_job_status(record: TaskRecord) -> PublicJobStatus:
    if record.status in {"queued", "scheduled", "interrupted"}:
        return "queued"
    if record.status in {"running", "waiting_confirmation"}:
        return "stopping" if record.stop_requested_at else "running"
    if record.status == "completed":
        return "succeeded"
    return "cancelled" if record.status == "cancelled" else "failed"


def job_summary(record: TaskRecord) -> JobSummary:
    return {
        "surface_id": record.surface_id,
        "chat_id": str(record.chat_id or ""),
        "detached": record.detached,
        "job_id": record.job_id,
        "status": public_job_status(record),
        "created_at": record.created_at,
        "started_at": record.started_at or None,
        "preview": str(record.input.get("text") or record.input.get("description") or "")[:160],
    }


def job_detail(record: TaskRecord) -> JobDetail:
    failed = public_job_status(record) == "failed"
    error = str(record.error_code or "execution_failed")
    safe_error = error if error.replace("_", "").replace("-", "").isalnum() and len(error) <= 80 else "execution_failed"
    return {
        **job_summary(record),
        "task": str(record.input.get("text") or record.input.get("description") or ""),
        "finished_at": record.finished_at or None,
        "result": record.result if record.status == "completed" else None,
        "error": safe_error if failed else None,
    }


def _record_to_payload(record: TaskRecord) -> dict[str, Any]:
    payload = dict(record.input)
    payload.update(record.metadata)
    payload.update(
        {
            "task_id": record.task_id,
            "id": record.task_id,
            "status": record.status,
            "chat_id": record.chat_id,
            "source": record.source,
            "correlation_id": record.correlation_id,
            "result": record.result,
            "result_ref": record.result_ref,
            "error_code": record.error_code,
            "ts": record.finished_at or record.started_at or record.created_at,
        }
    )
    return payload


def load_task_result(drive_root: Any, task_id: str) -> dict[str, Any] | None:
    store = runtime_persistence.current.tasks_repo
    record = store.get_task(str(task_id))
    if record is None:
        return None
    return _record_to_payload(record)


def list_task_results(
    drive_root: Any,
    *,
    statuses: list[str] | None = None,
    limit: int = 500,
    activity_first: bool = False,
    scope: OwnerScope | None = None,
) -> list[dict[str, Any]]:
    """Task payloads, narrowed to ``scope`` whenever the caller holds one.

    A caller that omits it reads across every owner, and the widening is invisible at the
    call site — which is how one owner's task descriptions and results reached another
    owner's prompt. Only a deliberate, named cross-owner reader may leave it out.
    """
    store = runtime_persistence.current.tasks_repo
    records = store.list_tasks(statuses=statuses, limit=limit, activity_first=activity_first, scope=scope)
    return [_record_to_payload(record) for record in records]


def write_task_result(
    drive_root: Any,
    task_id: str,
    status: str,
    *,
    scope: OwnerScope | None = None,
    execution_task_id: str | None = None,
    claim_token: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """Write a task status; new rows require the work's durable owner ``scope``."""
    store = runtime_persistence.current.tasks_repo
    task_id = str(task_id)
    existing = store.get_task(task_id)
    ts = str(fields.pop("ts", "") or (existing.finished_at if existing else "") or utc_now_iso())
    result = str(fields.pop("result", fields.pop("result_text", "")) or (existing.result if existing else ""))
    result_ref = str(fields.pop("result_ref", "") or (existing.result_ref if existing else ""))
    source = normalize_task_source(fields.pop("source", "") or (existing.source if existing else ""))
    correlation_id = str(fields.pop("correlation_id", "") or (existing.correlation_id if existing else ""))

    metadata = dict(existing.metadata if existing else {})
    metadata.update(fields)
    metadata.setdefault("ts", ts)

    if existing is not None:
        if existing.detached:
            if (
                existing.status == STATUS_RUNNING
                and status in TERMINAL_STATUSES
                and status != STATUS_REJECTED_DUPLICATE
            ):
                closed = store.finish_detached_attempt(
                    task_id,
                    status=status,  # type: ignore[arg-type]
                    result=result,
                    result_ref=result_ref,
                    error_code=str(fields.get("error_code") or ""),
                    metadata=metadata,
                    claim_token=str(fields.get("execution_claim") or ""),
                )
                if closed is not None:
                    return _record_to_payload(closed)
            current = store.get_task(task_id)
            return _record_to_payload(current) if current is not None else {}
        # A record already in flight moves through the guarded transition, never through a
        # full-row write: that write carries this caller's whole snapshot and would revert
        # every field it did not mean to touch. The absorbing rule is the statement's now,
        # so a late body that finds its task closed simply applies nothing — and re-writing
        # the same ordinary-task status still lands, which is how it records its trace.
        columns: dict[str, Any] = {"source": source, "correlation_id": correlation_id}
        if status in TERMINAL_STATUSES:
            # The instant is the caller's: a late body recording its trace against a closed
            # record must not stamp a new completion time on it.
            columns["finished_at"] = ts
        updated = store.update_task_status(
            task_id,
            status,  # type: ignore[arg-type]
            result=result,
            result_ref=result_ref,
            metadata={key: value for key, value in metadata.items() if existing.metadata.get(key) != value},
            **columns,
        )
        return _record_to_payload(updated if updated is not None else existing)
    else:
        if scope is None:
            raise ValueError(f"task {task_id!r} has no row yet and no scope to create one under")
        record = TaskRecord(
            task_id=task_id,
            # A status written for a task that has no row yet — a rejection, a request the
            # supervisor refused — belongs to the owner and agent of the work it records,
            # read from that work's scope and from nothing else.
            owner_id=scope.owner_id,
            agent_id=scope.agent_id,
            status=status,  # type: ignore[arg-type]
            source=source,
            correlation_id=correlation_id,
            task_type=str(metadata.get("task_type") or metadata.get("type") or "task"),
            surface_id=str(scope.surface_id),
            chat_id=scope.chat_id,
            input={"scope": scope.to_dict()},
            result=result,
            result_ref=result_ref,
            priority=int(metadata.get("priority") or 1),
            attempt=int(metadata.get("_attempt") or metadata.get("attempt") or 1),
            parent_task_id=str(metadata.get("parent_task_id") or ""),
            created_at=ts,
            finished_at=ts if status in TERMINAL_STATUSES else "",
            metadata=metadata,
        )

    # A creation, so the full-row write is this caller's own row rather than someone's
    # record in flight.
    store.upsert_task(record, execution_task_id=execution_task_id, claim_token=claim_token)
    return _record_to_payload(record)
