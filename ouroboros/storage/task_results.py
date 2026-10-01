"""Task status helpers backed by the runtime metadata store."""

from __future__ import annotations

from typing import Any

from ouroboros.storage.runtime_models import TaskRecord, normalize_task_source
from ouroboros.storage.runtime_store import get_runtime_store, init_runtime_store
from ouroboros.utils import utc_now_iso

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
            "conversation_id": record.conversation_id,
            "correlation_id": record.correlation_id,
            "result": record.result,
            "result_ref": record.result_ref,
            "ts": record.finished_at or record.started_at or record.created_at,
        }
    )
    return payload


def load_task_result(drive_root: Any, task_id: str) -> dict[str, Any] | None:
    store = get_runtime_store(drive_root) or init_runtime_store(drive_root)
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
) -> list[dict[str, Any]]:
    store = get_runtime_store(drive_root) or init_runtime_store(drive_root)
    records = store.list_tasks(statuses=statuses, limit=limit, activity_first=activity_first)
    return [_record_to_payload(record) for record in records]


def write_task_result(
    drive_root: Any,
    task_id: str,
    status: str,
    **fields: Any,
) -> dict[str, Any]:
    store = get_runtime_store(drive_root) or init_runtime_store(drive_root)
    task_id = str(task_id)
    existing = store.get_task(task_id)
    # This door reaches upsert_task directly, so it applies the store's absorbing-status rule
    # itself. Re-writing the same status still lands, which is how a late body records its trace.
    if existing is not None and existing.status in TERMINAL_STATUSES and status != existing.status:
        return _record_to_payload(existing)
    ts = str(fields.pop("ts", "") or (existing.finished_at if existing else "") or utc_now_iso())
    result = str(fields.pop("result", fields.pop("result_text", "")) or (existing.result if existing else ""))
    result_ref = str(fields.pop("result_ref", "") or (existing.result_ref if existing else ""))
    source = normalize_task_source(fields.pop("source", "") or (existing.source if existing else ""))
    conversation_id = str(fields.pop("conversation_id", "") or (existing.conversation_id if existing else ""))
    correlation_id = str(fields.pop("correlation_id", "") or (existing.correlation_id if existing else ""))
    chat_id = fields.get("chat_id")
    if chat_id in (None, "") and existing is not None:
        chat_id = existing.chat_id

    metadata = dict(existing.metadata if existing else {})
    metadata.update(fields)
    metadata.setdefault("ts", ts)

    if existing is not None:
        record = existing
        record.status = status  # type: ignore[assignment]
        record.source = source
        record.conversation_id = conversation_id
        record.correlation_id = correlation_id
        record.chat_id = chat_id
        record.result = result
        record.result_ref = result_ref
        record.metadata = metadata
        if status in TERMINAL_STATUSES:
            record.finished_at = ts
    else:
        record = TaskRecord(
            task_id=task_id,
            status=status,  # type: ignore[arg-type]
            source=source,
            conversation_id=conversation_id,
            correlation_id=correlation_id,
            task_type=str(metadata.get("task_type") or metadata.get("type") or "task"),
            chat_id=chat_id,
            input={},
            result=result,
            result_ref=result_ref,
            priority=int(metadata.get("priority") or 1),
            attempt=int(metadata.get("_attempt") or metadata.get("attempt") or 1),
            created_at=ts,
            finished_at=ts if status in TERMINAL_STATUSES else "",
            metadata=metadata,
        )

    store.upsert_task(record)
    return _record_to_payload(record)
