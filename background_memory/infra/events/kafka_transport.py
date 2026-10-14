"""The Kafka half of the seam: an `aiokafka` consumer and a `kafka-python` producer.

The deployment runs two client libraries on purpose. `kafka-python` has no awaitable consumer,
and a read that occupies the loop it is awaited on is exactly what this package's rule forbids —
so the reading half is `aiokafka`. Publishing stays where it is: five producing call sites
outside this package hand events over from threads, and `publish` owes them only that it never
blocks, which the synchronous producer already gives.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from aiokafka import TopicPartition
from aiokafka.errors import CommitFailedError, IllegalStateError, RebalanceInProgressError

from background_memory.infra.events.codec import (
    UnknownMemoryEventKind,
    decode_memory_event,
    encode_memory_event,
)
from background_memory.infra.events.outage import AsyncBrokerOutage, wait_out
from background_memory.interfaces import IMemoryEventConsumer, IMemoryEventPublisher, MemoryEvent
from infra.kafka_client import (
    KAFKA_SEND_TIMEOUT_SEC,
    KAFKA_ZERO_TIMEOUT_FETCH_MS,
    IKafkaClientFactory,
    KafkaOffsetReset,
    aiokafka_group_member,
    aiokafka_unanswered_errors,
    close_aiokafka_consumer,
    kafka_fetch_ms,
    kafka_unanswered_errors,
)
from new_core.identity import OwnerScope
from ouroboros.contracts.stream_replies import BROKER_REPORT_INTERVAL_SEC, BROKER_RETRY_SEC

if TYPE_CHECKING:
    from aiokafka import AIOKafkaConsumer, ConsumerRecord

log = logging.getLogger(__name__)

KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS = 300_000
MAX_SKIPPED_RECORDS_PER_POLL = 32

_PUBLISH_WAIT_SEC = KAFKA_SEND_TIMEOUT_SEC
_COMMIT_TIMEOUT_SEC = 1.0
_STALE_POSITION = (CommitFailedError, IllegalStateError, RebalanceInProgressError)
_UNANSWERED = aiokafka_unanswered_errors()
_UNPUBLISHABLE: tuple[type[Exception], ...] = (*kafka_unanswered_errors(), TypeError)


class KafkaMemoryEventPublisher(IMemoryEventPublisher):
    def __init__(self, *, clients: IKafkaClientFactory, topic: str) -> None:
        self._clients = clients
        self._topic = topic

    @staticmethod
    def record_key(scope: OwnerScope) -> bytes:
        return json.dumps([scope.owner_id, scope.agent_id], ensure_ascii=True, separators=(",", ":")).encode("ascii")

    def publish(self, event: MemoryEvent) -> None:
        try:
            value = json.dumps(encode_memory_event(event)).encode("utf-8")
            self._clients.producer(topic=self._topic).send(
                self._topic, key=self.record_key(event.scope), value=value
            ).get(timeout=_PUBLISH_WAIT_SEC)
        except _UNPUBLISHABLE:
            log.warning(
                "Memory event dropped: %s could not be published to %r scope=%s",
                type(event).__name__,
                self._topic,
                event.scope,
                exc_info=True,
            )


class KafkaMemoryEventConsumer(IMemoryEventConsumer):
    def __init__(
        self,
        *,
        clients: IKafkaClientFactory,
        topic: str,
        group_id: str,
        client_id: str,
        max_poll_interval_ms: int,
    ) -> None:
        self._clients = clients
        self._topic = topic
        self._group_id = group_id
        self._client_id = client_id
        self._max_poll_interval_ms = max_poll_interval_ms
        self._consumer: AIOKafkaConsumer | None = None
        self._joining: asyncio.Task[AIOKafkaConsumer] | None = None
        self._pending: dict[TopicPartition, int] = {}
        self._outage = AsyncBrokerOutage(
            logger=log, retry_after_sec=BROKER_RETRY_SEC, report_every_sec=BROKER_REPORT_INTERVAL_SEC
        )

    async def poll(self, timeout: float = 0.0) -> MemoryEvent | None:
        deadline = time.monotonic() + max(0.0, timeout)
        if self._outage.stands():
            await wait_out(deadline)
            return None
        try:
            consumer = await self._group_consumer(deadline)
            if consumer is None:
                return None
            await self._settle(consumer)
            event = await self._drain(consumer, deadline, KAFKA_ZERO_TIMEOUT_FETCH_MS if timeout <= 0 else 0)
        except _UNANSWERED as error:
            self._outage.record(
                "The memory-event topic %r of group %r cannot be read (%s); retrying every %.0fs",
                self._topic,
                self._group_id,
                type(error).__name__,
                BROKER_RETRY_SEC,
            )
            await wait_out(deadline)
            return None
        if not self._pending:
            self._read_again()
        return event

    async def close(self) -> None:
        """Release the group membership. Every path that abandons the consumer goes through here."""
        joining, self._joining = self._joining, None
        if joining is not None:
            joining.cancel()
            await asyncio.wait({joining})
            if not joining.cancelled() and joining.exception() is None:
                self._consumer = await joining
        consumer, self._consumer = self._consumer, None
        if consumer is None:
            return
        failure = await close_aiokafka_consumer(consumer)
        if failure is not None:
            log.warning("Could not close the memory-event consumer of group %r", self._group_id, exc_info=failure)

    async def _group_consumer(self, deadline: float) -> AIOKafkaConsumer | None:
        if self._consumer is not None:
            return self._consumer
        if self._joining is None:
            self._joining = asyncio.ensure_future(
                aiokafka_group_member(
                    self._clients,
                    topic=self._topic,
                    group_id=self._group_id,
                    client_id=self._client_id,
                    auto_offset_reset=KafkaOffsetReset.LATEST,
                    max_poll_interval_ms=self._max_poll_interval_ms,
                )
            )
        await asyncio.wait({self._joining}, timeout=max(0.0, deadline - time.monotonic()))
        if not self._joining.done():
            return None
        joining, self._joining = self._joining, None
        self._consumer = await joining
        return self._consumer

    async def _settle(self, consumer: AIOKafkaConsumer) -> None:
        if not self._pending:
            return
        try:
            await self._record_position(consumer, dict(self._pending))
        except _STALE_POSITION as stale:
            self._warn_stale(self._pending, stale)
        else:
            self._read_again()
        self._pending = {}

    async def _drain(self, consumer: AIOKafkaConsumer, deadline: float, floor_ms: int) -> MemoryEvent | None:
        skipped = 0
        while True:
            fetched = await consumer.getmany(timeout_ms=kafka_fetch_ms(deadline, floor_ms), max_records=1)
            floor_ms = 0
            records = [record for batch in fetched.values() for record in batch]
            if not records and time.monotonic() >= deadline:
                return None
            for record in records:
                event = await self._take(consumer, record)
                if event is not None:
                    return event
                skipped += 1
                if self._pending or skipped >= MAX_SKIPPED_RECORDS_PER_POLL:
                    return None

    async def _take(self, consumer: AIOKafkaConsumer, record: ConsumerRecord) -> MemoryEvent | None:
        position = {TopicPartition(record.topic, record.partition): record.offset + 1}
        try:
            await self._record_position(consumer, position)
        except _STALE_POSITION as stale:
            self._warn_stale(position, stale)
        except _UNANSWERED as error:
            self._pending = position
            log.warning(
                "Could not record the position past memory-event record %d of %s-%d in group %r (%s); it is "
                "recorded again before anything further is fetched",
                record.offset,
                record.topic,
                record.partition,
                self._group_id,
                type(error).__name__,
            )
        else:
            self._read_again()
        return self._decoded(record)

    @staticmethod
    async def _record_position(consumer: AIOKafkaConsumer, position: dict[TopicPartition, int]) -> None:
        await asyncio.wait_for(consumer.commit(position), timeout=_COMMIT_TIMEOUT_SEC)

    def _read_again(self) -> None:
        self._outage.cleared("The memory-event topic %r of group %r can be read again", self._topic, self._group_id)

    def _warn_stale(self, position: dict[TopicPartition, int], stale: Exception) -> None:
        log.warning(
            "Group %r refused the position %s as stale (%s) and it is dropped: the partition's next owner, this "
            "consumer included after a rebalance, may deliver that one event again",
            self._group_id,
            ", ".join(f"{tp.topic}-{tp.partition}@{offset}" for tp, offset in position.items()),
            type(stale).__name__,
        )

    def _decoded(self, record: ConsumerRecord) -> MemoryEvent | None:
        try:
            return decode_memory_event(json.loads(record.value))
        except UnknownMemoryEventKind as unknown:
            log.warning(
                "Skipping memory-event record %d of %s-%d: this build does not know the kind %r",
                record.offset,
                record.topic,
                record.partition,
                str(unknown),
            )
            return None
        except Exception:
            log.warning(
                "Dropping memory-event record %d of %s-%d: its value does not decode",
                record.offset,
                record.topic,
                record.partition,
                exc_info=True,
            )
            return None


__all__ = [
    "KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS",
    "MAX_SKIPPED_RECORDS_PER_POLL",
    "KafkaMemoryEventConsumer",
    "KafkaMemoryEventPublisher",
]
