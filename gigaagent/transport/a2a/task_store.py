"""A2A — TaskStore over the runtime database.

Each A2A task is one ``tasks`` row, written through ``runtime_persistence``.
"""
# mypy: ignore-errors

from __future__ import annotations

import logging
import time
from typing import Any

try:
    from a2a.server.context import ServerCallContext
    from a2a.server.tasks import TaskStore

    try:
        from a2a.types.a2a_pb2 import ListTasksRequest, ListTasksResponse  # type: ignore[import-not-found]
        from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE
    except ImportError:
        from a2a.types import TaskQueryParams as ListTasksRequest  # type: ignore[assignment]

        ListTasksResponse = None  # type: ignore[assignment]
        DEFAULT_LIST_TASKS_PAGE_SIZE = 100

    from transport.a2a.proto import (
        Task,
        is_terminal_task_state,
        task_from_dict,
        task_state_value,
        task_to_dict,
    )

    _A2A_AVAILABLE = True
except ImportError:
    _A2A_AVAILABLE = False
    TaskStore = object  # type: ignore[assignment,misc]
    Task = None  # type: ignore[assignment,misc]
    ServerCallContext = object  # type: ignore[assignment,misc]
    ListTasksRequest = ListTasksResponse = None  # type: ignore[assignment,misc]

log = logging.getLogger("a2a-server")


def _map_a2a_status(task_data: dict[str, Any]) -> str:
    from transport.a2a.proto import task_state_name

    state = (task_data.get("status") or {}).get("state", "")
    name = task_state_name(state)
    mapping = {
        "completed": "completed",
        "failed": "failed",
        "canceled": "cancelled",
        "cancelled": "cancelled",
        "rejected": "rejected_duplicate",
        "working": "running",
        "submitted": "queued",
        "input_required": "waiting_confirmation",
    }
    return mapping.get(name, "queued")


class RuntimeDbA2ATaskStore(TaskStore):
    """A2A task persistence in the runtime database."""

    def __init__(self, ttl_hours: int = 24):
        from ouroboros.storage.runtime_persistence import runtime_persistence

        self._store = runtime_persistence.current.tasks_repo
        self._ttl_hours = ttl_hours

    def _persist_task_dict(self, task_data: dict[str, Any]) -> None:
        from new_core.identity import ServiceJob
        from ouroboros.contracts.owner_scope import service_identity
        from ouroboros.storage.runtime_models import TaskRecord

        task_id = str(task_data.get("id") or "").strip()
        if not task_id:
            return
        # A protocol row nobody authored, not a scope minted from ``contextId``: that value
        # is routinely absent, an ``OwnerScope`` cannot hold an empty axis, and ``tasks`` has
        # no surface or chat column to receive one anyway.
        service = service_identity(job=ServiceJob.A2A_TASK_STORE)
        record = TaskRecord(
            task_id=task_id,
            owner_id=service.owner_id,
            agent_id=service.agent_id,
            source="a2a",
            status=_map_a2a_status(task_data),  # type: ignore[arg-type]
            task_type="a2a",
            metadata={"a2a_payload": task_data},
        )
        self._store.upsert_task(record)

    async def get(self, task_id: str, context: ServerCallContext | None = None) -> Task | None:
        del context
        record = self._store.get_task(task_id)
        if record is None or record.source != "a2a":
            return None
        payload = record.metadata.get("a2a_payload")
        if not isinstance(payload, dict):
            return None
        return task_from_dict(payload)

    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        del context
        self._persist_task_dict(task_to_dict(task))

    async def delete(self, task_id: str, context: ServerCallContext | None = None) -> None:
        del context
        record = self._store.get_task(task_id)
        if record is None:
            return
        self._store.cancel_task(task_id, result="A2A task deleted.")

    async def list(
        self,
        params: ListTasksRequest,
        context: ServerCallContext | None = None,
    ) -> ListTasksResponse:
        del context
        tasks: list[Task] = []
        for record in self._store.list_tasks(limit=5000):
            if record.source != "a2a":
                continue
            payload = record.metadata.get("a2a_payload")
            if not isinstance(payload, dict):
                continue
            task = task_from_dict(payload)
            if params.context_id and task.context_id != params.context_id:
                continue
            if params.status and task.status.state != params.status:
                continue
            tasks.append(task)

        page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        start = 0
        if params.page_token:
            try:
                start = max(0, int(params.page_token))
            except ValueError:
                start = 0
        page = tasks[start : start + page_size]
        next_token = ""
        if start + page_size < len(tasks):
            next_token = str(start + page_size)
        if ListTasksResponse is None:
            return {
                "tasks": [task_to_dict(task) for task in page],
                "nextPageToken": next_token or None,
            }
        return ListTasksResponse(tasks=page, next_page_token=next_token)

    async def cleanup_expired(self) -> int:
        """Reclaim every task past its TTL, terminal or not.

        A non-terminal task older than the TTL belongs to a process that is gone;
        keeping it would leave a row nothing ever removes.
        """
        cutoff = time.time() - self._ttl_hours * 3600
        removed = 0
        for record in self._store.list_tasks(limit=5000):
            if record.source != "a2a":
                continue
            finished = record.finished_at or record.created_at
            try:
                from datetime import datetime

                finished_ts = datetime.fromisoformat(str(finished).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                continue
            if finished_ts > cutoff:
                continue
            self._store.cancel_task(record.task_id, result="A2A task expired.")
            removed += 1
        return removed

    async def fail_orphaned_tasks(self) -> int:
        """Mark every stored non-terminal task failed: their process is gone.

        Runs at startup, before this process serves anything, so no task it owns
        can be caught by it.
        """
        failed = 0
        for record in self._store.list_tasks(limit=5000):
            if record.source != "a2a":
                continue
            payload = record.metadata.get("a2a_payload")
            if not isinstance(payload, dict):
                continue
            if is_terminal_task_state((payload.get("status") or {}).get("state", "")):
                continue
            try:
                task = task_from_dict(payload)
                task.status.state = task_state_value("failed")
                self._persist_task_dict(task_to_dict(task))
            except Exception:
                log.warning("Failed to fail the orphaned A2A task %s", record.task_id, exc_info=True)
                continue
            failed += 1
        if failed:
            log.info("A2A startup sweep failed %d task(s) left behind by a previous process", failed)
        return failed
