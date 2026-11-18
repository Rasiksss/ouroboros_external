"""Task record encoding shared with the remaining admission workflow."""

from __future__ import annotations

import uuid
from typing import Any

from new_core.identity import OwnerScope
from ouroboros.storage.message_field_crypto import decrypt_field, encrypt_field
from ouroboros.storage.runtime_models import TaskRecord, TaskRequest, TaskStatus
from ouroboros.storage.runtime_serialization import dump_json as _dump_json, load_json as _load_json
from ouroboros.utils import utc_now_iso

_RETIRED_KEY = "conversation_id"


_STATUS_EXTRA_COLUMNS = frozenset(
    {
        "attempt",
        "worker_id",
        "session_id",
        "source",
        "task_type",
        "correlation_id",
        "input_ref",
        "priority",
        "started_at",
        "finished_at",
    }
)


_LIVE_TASK_COLUMNS = (
    "status",
    "chat_id",
    "result",
    "result_ref",
    "error_code",
    "heartbeat_at",
    "parent_task_id",
    "runtime_id",
    "stop_requested_at",
    "soft_notice_at",
    "terminal_published_at",
    "terminal_publish_claim",
    "terminal_publish_after",
    *sorted(_STATUS_EXTRA_COLUMNS),
)


def _task_params(task: TaskRecord) -> dict[str, Any]:
    stored_scope = task.input.get("scope") or task.stored_scope()
    if stored_scope:
        scope = OwnerScope.from_dict(stored_scope)
        task.owner_id, task.agent_id = scope.owner_id, scope.agent_id
        task.surface_id, task.chat_id = str(scope.surface_id), scope.chat_id
        task.input = {**task.input, "scope": scope.to_dict(), "chat_id": scope.chat_id}
    input_json = encrypt_field(_dump_json(task.input))
    payload_json = encrypt_field(_dump_json(task.model_dump(mode="json")))
    return {
        "task_id": task.task_id,
        "job_id": task.job_id,
        "lineage_ordinal": task.lineage_ordinal,
        "detached": task.detached,
        "session_id": task.session_id,
        "owner_id": task.owner_id,
        "agent_id": task.agent_id,
        "source": task.source,
        "correlation_id": task.correlation_id,
        "status": task.status,
        "task_type": task.task_type,
        "surface_id": task.surface_id,
        "chat_id": str(task.chat_id or ""),
        "input_json": input_json,
        "input_ref": task.input_ref,
        "result": task.result,
        "result_ref": task.result_ref,
        "error_code": task.error_code,
        "priority": int(task.priority),
        "attempt": int(task.attempt),
        "worker_id": task.worker_id,
        "parent_task_id": task.parent_task_id,
        "runtime_id": task.runtime_id,
        "stop_requested_at": task.stop_requested_at,
        "soft_notice_at": task.soft_notice_at,
        "terminal_published_at": task.terminal_published_at,
        "terminal_publish_claim": task.terminal_publish_claim,
        "terminal_publish_after": task.terminal_publish_after,
        "queued_at": task.queued_at,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "heartbeat_at": task.heartbeat_at,
        "finished_at": task.finished_at,
        "metadata_json": _dump_json(task.metadata),
        "payload_json": payload_json,
    }


def _row_to_task(row: Any) -> TaskRecord:
    mapping = row._mapping
    payload = _load_json(decrypt_field(mapping["payload_json"]))
    overlaid = _overlay_axes(payload if isinstance(payload, dict) else {}, mapping, "owner_id", "agent_id")
    overlaid.setdefault("input", _load_json(decrypt_field(mapping["input_json"])))
    overlaid["metadata"] = _load_json(mapping["metadata_json"])
    for column in (
        *_LIVE_TASK_COLUMNS,
        "task_id",
        "job_id",
        "lineage_ordinal",
        "detached",
        "surface_id",
        "queued_at",
        "created_at",
    ):
        stored = mapping.get(f"logical_{column}", mapping[column])
        # Preserve the payload's chat-id type when it agrees with the authoritative column.
        if overlaid.get(column) is not None and stored is not None and str(overlaid[column]) == str(stored):
            continue
        overlaid[column] = stored
    record = TaskRecord.model_validate(overlaid)
    scope = record.stored_scope()
    if scope is not None:
        record.input = {**record.input, "scope": scope, "chat_id": record.chat_id}
    return record


def _task_from_request(request: TaskRequest, *, status: TaskStatus) -> TaskRecord:
    task_id = request.id or uuid.uuid4().hex[:8]
    now = utc_now_iso()
    if not request.queued_at:
        request.queued_at = now
    if not request.id:
        request.id = task_id
    scope = OwnerScope.from_dict(request.scope)
    task = TaskRecord(
        task_id=task_id,
        job_id=request.job_id or task_id,
        detached=request.detached,
        owner_id=scope.owner_id,
        agent_id=scope.agent_id,
        source=request.source,
        correlation_id=request.correlation_id,
        status=status,
        task_type=request.type,
        chat_id=scope.chat_id,
        surface_id=str(scope.surface_id),
        input=request.to_task_dict(),
        parent_task_id=request.parent_task_id,
        priority=request.priority,
        attempt=max(1, int(request.attempt or 1)),
        queued_at=request.queued_at,
        created_at=now,
    )
    # Status helpers compose runtime storage; defer the import to avoid a bootstrap cycle.
    from ouroboros.storage.task_results import TERMINAL_STATUSES

    if status in TERMINAL_STATUSES:
        task.finished_at = now
    return task


def _overlay_axes(payload: dict[str, Any], mapping: Any, *axes: str) -> dict[str, Any]:
    """The columns are authoritative; the retired key never comes back."""
    payload.pop(_RETIRED_KEY, None)
    payload.update({axis: mapping[axis] for axis in axes})
    return payload
