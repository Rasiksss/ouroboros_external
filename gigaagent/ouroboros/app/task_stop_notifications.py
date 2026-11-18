"""In-process and Redis Streams adapters for best-effort task-stop hints.

The wire shape a hint takes is here and not per backend: the Kafka adapter
(`ouroboros/app/task_stop_kafka.py`) carries the same fields as one JSON object,
so a fleet reading one broker decodes what the other would have written.
"""

from __future__ import annotations

import json
import logging
import queue
import time
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any, cast

import redis

from new_core.coordination.interfaces import ITaskStopConsumer, ITaskStopPublisher, TaskStopNotice
from ouroboros.contracts.stream_replies import block_ms, row, text
from ouroboros.storage.runtime_models import TaskRecord
from transport.interfaces import ITaskStopNotifier

log = logging.getLogger(__name__)

_MAX_STREAM_ENTRIES = 1_024
MAX_TASK_IDS = 256
_POLL_SLICE_MS = 250


class TaskStopNotifier(ITaskStopNotifier):
    def __init__(self, publisher: ITaskStopPublisher | None) -> None:
        self._publisher = publisher

    def notify(self, records: Iterable[TaskRecord]) -> None:
        """Notify the addressed runtimes after durable stop intent has committed."""
        if self._publisher is None:
            return
        by_runtime: dict[str, list[str]] = {}
        for record in records:
            if record.status == "running" and record.runtime_id:
                by_runtime.setdefault(record.runtime_id, []).append(record.task_id)
        for runtime_id, task_ids in by_runtime.items():
            try:
                self._publisher.publish(TaskStopNotice(runtime_id=runtime_id, task_ids=tuple(task_ids)))
            except Exception:
                log.warning("Failed to publish task-stop hint for runtime %s", runtime_id, exc_info=True)


class InProcessTaskStopPublisher(ITaskStopPublisher):
    def __init__(self, messages: queue.Queue[TaskStopNotice]) -> None:
        self._messages = messages

    def publish(self, notice: TaskStopNotice) -> None:
        self._messages.put_nowait(notice)


class InProcessTaskStopConsumer(ITaskStopConsumer):
    def __init__(self, messages: queue.Queue[TaskStopNotice]) -> None:
        self._messages = messages

    def start(self) -> bool:
        """The in-process queue is ready when it is constructed."""
        return True

    def poll(self, timeout: float = 0.0) -> TaskStopNotice | None:
        try:
            return self._messages.get(timeout=max(0.0, float(timeout)))
        except queue.Empty:
            return None


def create_task_stop_transport() -> tuple[ITaskStopPublisher, ITaskStopConsumer]:
    messages: queue.Queue[TaskStopNotice] = queue.Queue()
    return InProcessTaskStopPublisher(messages), InProcessTaskStopConsumer(messages)


class RedisTaskStopPublisher(ITaskStopPublisher):
    def __init__(self, client: redis.Redis, *, stream: str) -> None:
        self._client = client
        self._stream = str(stream)

    def publish(self, notice: TaskStopNotice) -> None:
        self._client.xadd(
            self._stream,
            cast(dict[Any, Any], encode_task_stop_fields(notice)),
            maxlen=_MAX_STREAM_ENTRIES,
            approximate=True,
        )


class RedisTaskStopConsumer(ITaskStopConsumer):
    """Read new stream entries independently, so every runtime sees every hint."""

    def __init__(self, client: redis.Redis, *, stream: str) -> None:
        self._client = client
        self._stream = str(stream)
        self._last_id: str | None = None
        self._pending: deque[TaskStopNotice] = deque()

    def start(self) -> bool:
        """Anchor the stream before this runtime may hold an addressed task."""
        if self._last_id is not None:
            return True
        try:
            latest = self._client.xrevrange(self._stream, count=1)
        except redis.RedisError:
            self._last_id = "0-0"
            return False
        self._last_id = str(text(latest[0][0])) if latest else "0-0"
        return True

    def poll(self, timeout: float = 0.0) -> TaskStopNotice | None:
        if self._pending:
            return self._pending.popleft()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            wait_ms = block_ms(deadline, _POLL_SLICE_MS)
            kwargs = {"count": MAX_TASK_IDS}
            if wait_ms:
                kwargs["block"] = wait_ms
            try:
                if self._last_id is None:
                    self.start()
                last_id = self._last_id
                if last_id is None:
                    return None
                response = self._client.xread({self._stream: last_id}, **kwargs)
            except redis.RedisError:
                log.warning("Task-stop stream %s could not be read", self._stream, exc_info=True)
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                return None
            for entry_id, fields in _entries(response):
                self._last_id = entry_id
                notice = decode_task_stop_notice(fields)
                if notice is not None:
                    self._pending.append(notice)
            if self._pending:
                return self._pending.popleft()
            if time.monotonic() >= deadline or not wait_ms:
                return None


def _entries(response: Any) -> list[tuple[str, Mapping[Any, Any]]]:
    entries: list[tuple[str, Mapping[Any, Any]]] = []
    streams = response.values() if isinstance(response, dict) else (items for _name, items in response or ())
    for items in streams:
        for entry_id, fields in items or ():
            if fields is not None:
                entries.append((str(text(entry_id)), fields))
    return entries


def encode_task_stop_fields(notice: TaskStopNotice) -> dict[str, str]:
    """The one wire shape of a hint, whichever broker carries it."""
    return {
        "version": str(notice.version),
        "runtime_id": str(notice.runtime_id),
        "task_ids": json.dumps(list(notice.task_ids), ensure_ascii=True, separators=(",", ":")),
    }


def decode_task_stop_notice(fields: Mapping[Any, Any]) -> TaskStopNotice | None:
    decoded = row(fields)
    try:
        version = int(decoded.get("version") or 0)
        runtime_id = str(decoded.get("runtime_id") or "").strip()
        raw_task_ids = json.loads(str(decoded.get("task_ids") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if version != 1 or not runtime_id or not isinstance(raw_task_ids, list):
        return None
    task_ids = tuple(str(value).strip() for value in raw_task_ids[:MAX_TASK_IDS] if str(value).strip())
    return TaskStopNotice(runtime_id=runtime_id, task_ids=task_ids, version=version) if task_ids else None


__all__ = [
    "MAX_TASK_IDS",
    "InProcessTaskStopConsumer",
    "InProcessTaskStopPublisher",
    "RedisTaskStopConsumer",
    "RedisTaskStopPublisher",
    "TaskStopNotifier",
    "create_task_stop_transport",
    "decode_task_stop_notice",
    "encode_task_stop_fields",
]
