"""The asynchronous consumer of the Kafka double, in `aiokafka`'s own surface.

One cluster, two client libraries — the double reproduces each library's own names, signatures
and error classes rather than unifying them, because a backend written against a unified double
compiles against neither. Everything durable lives in `KafkaDoubleCluster`; this face only
translates.

The waiting is the whole reason this is not a wrapper around `DoubleGroupConsumer.poll`: that one
blocks on a `threading.Condition`, which on a loop is the defect the real client exists to avoid.
Here every fetch asks the cluster for what it already holds and then yields.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiokafka.errors as AsyncErrors
from aiokafka import ConsumerRecord as AsyncConsumerRecord, TopicPartition as AsyncTopicPartition
from kafka.structs import OffsetAndMetadata, TopicPartition

from infra.kafka_client import KafkaOffsetReset
from tests.helpers.kafka_double_clients import (
    WAIT_SLICE_SEC,
    DoubleGroupConsumer,
    DoubleProducer,
    deadline_after,
    expired,
)

_STOPPED = "Consumer is closed"


class DoubleAsyncProducer:
    def __init__(self, *, cluster):
        self.cluster = cluster
        self.inner = DoubleProducer(cluster=cluster)
        self.started = self.stopped = False

    async def start(self):
        while True:
            with self.cluster._changed:
                if not self.cluster._outage:
                    self.started = True
                    return
            await asyncio.sleep(WAIT_SLICE_SEC)

    async def send_and_wait(self, topic, value=None, key=None, partition=None):
        if not self.started or self.stopped:
            raise RuntimeError("producer is not started")
        while True:
            with self.cluster._changed:
                if topic in self.cluster._topics:
                    future = self.cluster._send(self.inner, topic, partition or 0, key, value)
                    break
            await asyncio.sleep(WAIT_SLICE_SEC)
        ready = asyncio.Event()
        loop = asyncio.get_running_loop()
        future.add_both(lambda _: loop.call_soon_threadsafe(ready.set))
        await ready.wait()
        if future.failed():
            error = future.exception
            translated = getattr(AsyncErrors, type(error).__name__, type(error))
            raise translated(str(error))
        return future.value

    async def stop(self):
        while True:
            with self.cluster._changed:
                if not self.cluster._holds_sends_of(self.inner):
                    self.stopped = True
                    return
            await asyncio.sleep(WAIT_SLICE_SEC)


def _async_record(record: Any) -> AsyncConsumerRecord:
    return AsyncConsumerRecord(
        topic=record.topic,
        partition=record.partition,
        offset=record.offset,
        timestamp=record.timestamp,
        timestamp_type=record.timestamp_type,
        key=record.key,
        value=record.value,
        checksum=record.checksum,
        serialized_key_size=record.serialized_key_size,
        serialized_value_size=record.serialized_value_size,
        headers=tuple(record.headers or ()),
    )


def _offsets(offsets: Any) -> dict[TopicPartition, OffsetAndMetadata]:
    """`aiokafka` takes `{tp: offset}` or `{tp: (offset, metadata)}`; the cluster keeps triples."""
    if offsets is None:
        return {}
    translated: dict[TopicPartition, OffsetAndMetadata] = {}
    for partition, value in offsets.items():
        if not isinstance(partition, AsyncTopicPartition | TopicPartition):
            raise ValueError("offsets must be keyed by TopicPartition")
        if isinstance(value, int):
            offset, metadata = value, ""
        elif isinstance(value, tuple) and len(value) == 2:
            offset, metadata = value
        else:
            raise ValueError("offsets must be an offset or an (offset, metadata) pair")
        translated[TopicPartition(partition.topic, partition.partition)] = OffsetAndMetadata(
            int(offset), str(metadata), -1
        )
    return translated


class DoubleAsyncGroupConsumer:
    """`AIOKafkaConsumer` over the double's cluster: `start`/`stop`, `getmany`, `commit`."""

    def __init__(
        self,
        *,
        cluster: Any,
        group_id: str,
        client_id: str,
        reset: KafkaOffsetReset,
        max_poll_interval_ms: int,
    ) -> None:
        self._inner = DoubleGroupConsumer(
            cluster=cluster,
            group_id=group_id,
            client_id=client_id,
            reset=reset,
            max_poll_interval_ms=max_poll_interval_ms,
        )
        self._started = False

    @property
    def wrapped_group_consumer(self) -> DoubleGroupConsumer:
        """What a case reads an assignment off — the cluster's own member record."""
        return self._inner

    @property
    def client_id(self) -> str:
        return self._inner.client_id

    def subscribe(self, topics=(), pattern=None, listener=None) -> None:
        self._inner.subscribe(topics=topics, pattern=pattern, listener=listener)

    def subscription(self):
        return self._inner.subscription()

    def assignment(self):
        return {AsyncTopicPartition(tp.topic, tp.partition) for tp in self._inner.assignment()}

    async def start(self) -> None:
        assert not self._started, "Did you call `start` twice?"
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._inner.close(autocommit=False, timeout_ms=0)

    async def getmany(self, *partitions, timeout_ms=0, max_records=None) -> dict[AsyncTopicPartition, list[Any]]:
        self._refuse_stopped()
        assert timeout_ms >= 0, "timeout_ms must not be negative"
        deadline = deadline_after(timeout_ms)
        while True:
            fetched = self._inner.poll(timeout_ms=0, max_records=1 if max_records is None else max_records)
            if fetched:
                return {
                    AsyncTopicPartition(tp.topic, tp.partition): [_async_record(record) for record in batch]
                    for tp, batch in fetched.items()
                }
            if expired(deadline) or timeout_ms == 0:
                return {}
            await asyncio.sleep(WAIT_SLICE_SEC)
            self._refuse_stopped()

    async def getone(self, *partitions) -> Any:
        while True:
            fetched = await self.getmany(*partitions, timeout_ms=1_000)
            for batch in fetched.values():
                for record in batch:
                    return record

    async def commit(self, offsets=None) -> None:
        """No per-call timeout in this library: a cluster that does not answer answers no.

        The real client retries inside `request_timeout_ms`; the double does not emulate a
        retry budget, it emulates the decision — an unanswered commit raises, the caller
        records the outage and keeps the position pending, which is the path under test.
        """
        self._refuse_stopped()
        try:
            self._inner.commit(_offsets(offsets) or None, timeout_ms=1)
        except Exception as failure:
            raise _translated(failure) from failure

    async def position(self, partition) -> int | None:
        self._refuse_stopped()
        return self._inner._positions.get(TopicPartition(partition.topic, partition.partition))

    def seek(self, partition, offset) -> None:
        self._inner.seek(TopicPartition(partition.topic, partition.partition), offset)

    def _refuse_stopped(self) -> None:
        if not self._started:
            raise AsyncErrors.ConsumerStoppedError(_STOPPED)


def _translated(failure: BaseException) -> BaseException:
    """The cluster raises `kafka-python`'s classes; this face raises the other library's."""
    import kafka.errors as Errors

    if isinstance(failure, Errors.CommitFailedError):
        return AsyncErrors.CommitFailedError(*failure.args)
    if isinstance(failure, Errors.RebalanceInProgressError):
        return AsyncErrors.RebalanceInProgressError(*failure.args)
    if isinstance(failure, Errors.KafkaTimeoutError):
        return AsyncErrors.KafkaTimeoutError(*failure.args)
    if isinstance(failure, Errors.KafkaError):
        return AsyncErrors.KafkaError(*failure.args)
    return failure


__all__ = ["DoubleAsyncGroupConsumer"]
