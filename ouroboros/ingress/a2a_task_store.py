"""
A2A — File-based TaskStore.

Stores each A2A task via StorageProvider under scope ``a2a_tasks``.
Uses atomic writes (write -> rename) on file backend.
"""
# mypy: ignore-errors

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import time
import uuid
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

    from ouroboros.ingress.a2a_proto import (
        Task,
        is_terminal_task_state,
        task_from_dict,
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


def _default_context(context: ServerCallContext | None) -> ServerCallContext:
    if context is not None:
        return context
    return ServerCallContext()


def _safe_task_filename(task_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", task_id)
    safe_id = pathlib.PurePosixPath(safe_id).name or "invalid_task_id"
    if not safe_id.strip("."):
        safe_id = "invalid_task_id"
    return f"{safe_id}.json"


class FileTaskStore(TaskStore):
    """Task persistence backed by StorageProvider (file or S3)."""

    def __init__(self, data_dir: pathlib.Path, ttl_hours: int = 24):
        from ouroboros.storage.blob.drive import DriveStorage
        from ouroboros.storage.blob.factory import create_storage_provider, resolve_storage_backend
        from ouroboros.storage.blob.scopes import SCOPE_A2A_TASKS

        root = pathlib.Path(data_dir).resolve()
        provider = create_storage_provider(data_dir=root, backend=resolve_storage_backend())
        self._drive = DriveStorage(root, provider)
        self._scope = SCOPE_A2A_TASKS
        self._data_dir = pathlib.Path(data_dir)
        self._ttl_hours = ttl_hours
        # Legacy local dir for file backend mkdir
        self._dir = self._data_dir / "a2a_tasks"
        if self._drive.is_file_backend():
            self._dir.mkdir(parents=True, exist_ok=True)

    def _task_key(self, task_id: str) -> str:
        return _safe_task_filename(task_id)

    def _task_path(self, task_id: str) -> pathlib.Path:
        return self._dir / _safe_task_filename(task_id)

    async def get(self, task_id: str, context: ServerCallContext | None = None) -> Task | None:
        del context
        key = self._task_key(task_id)
        if not self._drive.provider.exists(self._scope, key):
            return None
        try:
            raw = self._drive.provider.get_text(self._scope, key)
            if raw is None:
                return None
            data = json.loads(raw)
            return task_from_dict(data)
        except Exception:
            log.warning("Failed to read task %s", task_id, exc_info=True)
            return None

    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        del context
        key = self._task_key(task.id)
        content = json.dumps(task_to_dict(task), ensure_ascii=False, indent=2)
        path = self._task_path(task.id)
        if self._drive.is_file_backend():
            tmp = path.with_name(f".{path.name}.tmp.{uuid.uuid4().hex[:8]}")
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, content.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(str(tmp), str(path))
            return
        self._drive.provider.put_text(self._scope, key, content, content_type="application/json")

    async def delete(self, task_id: str, context: ServerCallContext | None = None) -> None:
        del context
        key = self._task_key(task_id)
        self._drive.provider.delete(self._scope, key)
        if self._drive.is_file_backend():
            try:
                self._task_path(task_id).unlink(missing_ok=True)
            except Exception:
                log.warning("Failed to delete local task file %s", task_id, exc_info=True)

    async def list(
        self,
        params: ListTasksRequest,
        context: ServerCallContext | None = None,
    ) -> Any:
        del context
        tasks: list[Task] = []
        for key in sorted(self._drive.provider.list_keys(self._scope, "")):
            if not key.endswith(".json"):
                continue
            try:
                raw = self._drive.provider.get_text(self._scope, key)
                if raw is None:
                    continue
                data = json.loads(raw)
                task = task_from_dict(data)
            except Exception:
                continue
            if params.context_id and task.context_id != params.context_id:
                continue
            if params.status and task.status.state != params.status:
                continue
            tasks.append(task)

        page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        start = 0
        if params.page_token:
            try:
                start = int(params.page_token)
            except ValueError:
                start = 0
        page = tasks[start : start + page_size]
        next_token = str(start + page_size) if start + page_size < len(tasks) else ""
        return ListTasksResponse(tasks=page, next_page_token=next_token)

    async def cleanup_expired(self) -> int:
        cutoff = time.time() - self._ttl_hours * 3600
        removed = 0
        try:
            if self._drive.is_file_backend():
                for path in self._dir.glob("*.json"):
                    if path.stat().st_mtime > cutoff:
                        continue
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                        state = (data.get("status") or {}).get("state", "")
                        if is_terminal_task_state(state):
                            path.unlink(missing_ok=True)
                            removed += 1
                    except Exception:
                        continue
                return removed
            for key in self._drive.provider.list_keys(self._scope, ""):
                if not key.endswith(".json"):
                    continue
                try:
                    raw = self._drive.provider.get_text(self._scope, key)
                    if raw is None:
                        continue
                    data = json.loads(raw)
                    state = (data.get("status") or {}).get("state", "")
                    if is_terminal_task_state(state):
                        task_id = str(data.get("id") or key.replace(".json", ""))
                        await self.delete(task_id)
                        removed += 1
                except Exception:
                    continue
        except Exception:
            log.warning("Task cleanup error", exc_info=True)
        return removed


def _map_a2a_status(task_data: dict[str, Any]) -> str:
    from ouroboros.ingress.a2a_proto import task_state_name

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


class SQLiteA2ATaskStore(TaskStore):
    """SQLite-backed A2A task persistence via the runtime store."""

    def __init__(self, data_dir: pathlib.Path, ttl_hours: int = 24):
        from ouroboros.storage.runtime_store import get_runtime_store, init_runtime_store

        self._data_dir = pathlib.Path(data_dir)
        self._store = get_runtime_store(self._data_dir) or init_runtime_store(self._data_dir)
        self._ttl_hours = ttl_hours

    def _persist_task_dict(self, task_data: dict[str, Any]) -> None:
        from ouroboros.storage.runtime_models import TaskRecord

        task_id = str(task_data.get("id") or "").strip()
        if not task_id:
            return
        record = TaskRecord(
            task_id=task_id,
            conversation_id=str(task_data.get("contextId") or task_data.get("context_id") or ""),
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
            if is_terminal_task_state((record.metadata.get("a2a_payload") or {}).get("status", {}).get("state", "")):
                self._store.cancel_task(record.task_id, result="A2A task expired.")
                removed += 1
        return removed


def create_task_store(data_dir: pathlib.Path, ttl_hours: int = 24) -> FileTaskStore:
    return FileTaskStore(data_dir, ttl_hours=ttl_hours)
