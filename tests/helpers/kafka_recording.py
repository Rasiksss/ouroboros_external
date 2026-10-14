from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from infra.kafka_client import IKafkaClientFactory, KafkaOffsetReset


@dataclass(frozen=True)
class RecordedCall:
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


class RecordingClient:
    def __init__(self, *, wrapped: Any) -> None:
        self.wrapped = wrapped
        self.calls: list[RecordedCall] = []
        self._armed: dict[str, BaseException] = {}
        self._lock = threading.Lock()

    def fail_next(self, method: str, error: BaseException) -> None:
        with self._lock:
            self._armed[method] = error

    def calls_of(self, method: str) -> list[RecordedCall]:
        with self._lock:
            return [call for call in self.calls if call.method == method]

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self.wrapped, name)
        if not callable(attribute):
            return attribute

        def _recorded(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                self.calls.append(RecordedCall(name, args, dict(kwargs)))
                error = self._armed.pop(name, None)
            if error is not None:
                raise error
            return attribute(*args, **kwargs)

        return _recorded


@dataclass(frozen=True)
class RecordedClient:
    accessor: str
    kwargs: dict[str, Any]
    client: RecordingClient


class KafkaRecordingClientFactory(IKafkaClientFactory):
    def __init__(self, *, clients: IKafkaClientFactory) -> None:
        self.wrapped = clients
        self.built: list[RecordedClient] = []
        self._proxies: dict[int, RecordingClient] = {}
        self._lock = threading.Lock()

    def producer(self, *, topic: str) -> Any:
        return self._record("producer", {"topic": topic}, self.wrapped.producer(topic=topic))

    def async_producer(self) -> Any:
        return self._record("async_producer", {}, self.wrapped.async_producer())

    def group_consumer(
        self, *, group_id: str, client_id: str, auto_offset_reset: KafkaOffsetReset, max_poll_interval_ms: int
    ) -> Any:
        kwargs = {
            "group_id": group_id,
            "client_id": client_id,
            "auto_offset_reset": auto_offset_reset,
            "max_poll_interval_ms": max_poll_interval_ms,
        }
        return self._record("group_consumer", kwargs, self.wrapped.group_consumer(**kwargs))

    def async_group_consumer(
        self, *, group_id: str, client_id: str, auto_offset_reset: KafkaOffsetReset, max_poll_interval_ms: int
    ) -> Any:
        kwargs = {
            "group_id": group_id,
            "client_id": client_id,
            "auto_offset_reset": auto_offset_reset,
            "max_poll_interval_ms": max_poll_interval_ms,
        }
        return self._record("async_group_consumer", kwargs, self.wrapped.async_group_consumer(**kwargs))

    def partition_reader(self, *, auto_offset_reset: KafkaOffsetReset) -> Any:
        kwargs = {"auto_offset_reset": auto_offset_reset}
        return self._record("partition_reader", kwargs, self.wrapped.partition_reader(**kwargs))

    def clients_of(self, accessor: str) -> list[RecordedClient]:
        with self._lock:
            return [recorded for recorded in self.built if recorded.accessor == accessor]

    def _record(self, accessor: str, kwargs: dict[str, Any], client: Any) -> RecordingClient:
        with self._lock:
            proxy = self._proxies.get(id(client))
            if proxy is None or proxy.wrapped is not client:
                proxy = RecordingClient(wrapped=client)
                self._proxies[id(client)] = proxy
            self.built.append(RecordedClient(accessor, kwargs, proxy))
            return proxy


__all__ = ["KafkaRecordingClientFactory", "RecordedCall", "RecordedClient", "RecordingClient"]
