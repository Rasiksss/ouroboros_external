from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from kafka.errors import CommitFailedError, KafkaTimeoutError, RebalanceInProgressError
from kafka.structs import OffsetAndMetadata, TopicPartition

from infra.kafka_client import (
    KAFKA_CLOSE_TIMEOUT_MS,
    KAFKA_JOIN_TIMEOUT_SEC,
    KAFKA_SEND_TIMEOUT_SEC,
    KAFKA_ZERO_TIMEOUT_FETCH_MS,
    IKafkaClientFactory,
    KafkaOffsetReset,
    close_kafka_consumer,
    kafka_fetch_ms,
    kafka_group_member,
    kafka_unanswered_errors,
)

if TYPE_CHECKING:
    from aiokafka import AIOKafkaProducer
from ouroboros.contracts.stream_replies import BROKER_REPORT_INTERVAL_SEC, BROKER_RETRY_SEC, BrokerOutage
from ouroboros.utils import finish_file_io
from transport.codec import decode_inbound, decode_outbound, encode_inbound, encode_outbound, ensure_wire_safe
from transport.infra.pending import PendingDelivery
from transport.interfaces import (
    DeliveredMessage,
    IInbox,
    IMessageGateway,
    InboundEnvelope,
    IOutbox,
    IOutboxReader,
    OutboundMessage,
    SubscriptionMode,
)

log = logging.getLogger(__name__)

KAFKA_INBOX_MAX_POLL_INTERVAL_MS = 300_000
MAX_SKIPPED_RECORDS_PER_POLL = 32
_MAX_PENDING_PER_READER = 10_000

_SEND_TIMEOUT_SEC = KAFKA_SEND_TIMEOUT_SEC
_SETTLE_TIMEOUT_MS = 1_000
_READER_START_TIMEOUT_MS = 5_000
_READER_START_ATTEMPT_MS = 500
_CLOSE_TIMEOUT_MS = KAFKA_CLOSE_TIMEOUT_MS
_PARTITION = 0
_UNANSWERED = kafka_unanswered_errors()


def _inbound_value(envelope: InboundEnvelope) -> bytes:
    if not isinstance(envelope, InboundEnvelope):
        raise TypeError(f"inbox takes an InboundEnvelope, got {type(envelope).__name__}")
    ensure_wire_safe(envelope)
    return json.dumps(encode_inbound(envelope)).encode("utf-8")


def _outbound_value(message: OutboundMessage) -> bytes:
    if not isinstance(message, OutboundMessage):
        raise TypeError(f"outbox takes an OutboundMessage, got {type(message).__name__}")
    ensure_wire_safe(message)
    return json.dumps(encode_outbound(message)).encode("utf-8")


def _records(fetched: Mapping[TopicPartition, list[Any]]) -> list[Any]:
    return [record for batch in fetched.values() for record in batch]


def _unexpired(remaining: float) -> float:
    if remaining <= 0:
        raise TimeoutError("outbox publication deadline expired")
    return remaining


def _release(consumer: Any, *, name: str, topic: str) -> None:
    failure = close_kafka_consumer(consumer, timeout_ms=_CLOSE_TIMEOUT_MS)
    if failure is not None:
        log.warning("Could not close the consumer of outbound reader %r on %r", name, topic, exc_info=failure)


class KafkaInbox(IInbox):
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
        self._outage = BrokerOutage(
            logger=log, retry_after_sec=BROKER_RETRY_SEC, report_every_sec=BROKER_REPORT_INTERVAL_SEC
        )
        self._consumer: Any = None
        self._unsettled: tuple[TopicPartition, int] | None = None

    def submit(self, envelope: InboundEnvelope) -> None:
        value = _inbound_value(envelope)
        self._clients.producer(topic=self._topic).send(self._topic, value=value, partition=_PARTITION).get(
            timeout=_SEND_TIMEOUT_SEC
        )

    def poll(self, timeout: float = 0.0) -> InboundEnvelope | None:
        deadline = time.monotonic() + max(0.0, timeout)
        if self._outage.stands(deadline):
            return None
        try:
            consumer = self._member()
            self._settle_previous(consumer)
            envelope = self._fetch(consumer, deadline, KAFKA_ZERO_TIMEOUT_FETCH_MS if timeout <= 0 else 0)
        except _UNANSWERED as error:
            self._outage.record(
                deadline,
                "The inbound topic %r cannot be read by group %r (%s); retrying every %.0fs. The tick keeps "
                "running and the envelopes already submitted are drained when the broker answers again.",
                self._topic,
                self._group_id,
                error,
                BROKER_RETRY_SEC,
            )
            return None
        self._outage.cleared("The inbound topic %r can be read by group %r again", self._topic, self._group_id)
        return envelope

    def _member(self) -> Any:
        if self._consumer is None:
            self._consumer = kafka_group_member(
                self._clients,
                topic=self._topic,
                group_id=self._group_id,
                client_id=self._client_id,
                auto_offset_reset=KafkaOffsetReset.EARLIEST,
                max_poll_interval_ms=self._max_poll_interval_ms,
            )
        return self._consumer

    def _settle_previous(self, consumer: Any) -> None:
        if self._unsettled is None:
            return
        partition, offset = self._unsettled
        try:
            consumer.commit({partition: OffsetAndMetadata(offset + 1, "", -1)}, timeout_ms=_SETTLE_TIMEOUT_MS)
        except (CommitFailedError, RebalanceInProgressError) as error:
            self._unsettled = None
            log.info(
                "Dropped the stale settlement of offset %d on %r for group %r (%s): the broker no longer counts "
                "this member, so the envelope is redelivered after the rejoin and collapses on its task id",
                offset,
                self._topic,
                self._group_id,
                type(error).__name__,
            )
            return
        self._unsettled = None

    def _fetch(self, consumer: Any, deadline: float, floor_ms: int) -> InboundEnvelope | None:
        skipped = 0
        while True:
            records = _records(consumer.poll(timeout_ms=kafka_fetch_ms(deadline, floor_ms), max_records=1))
            floor_ms = 0
            for record in records:
                self._unsettled = (TopicPartition(record.topic, record.partition), record.offset)
                envelope = self._decoded(record)
                if envelope is not None:
                    return envelope
                skipped += 1
                if skipped >= MAX_SKIPPED_RECORDS_PER_POLL:
                    return None
            if not records and time.monotonic() >= deadline:
                return None

    def _decoded(self, record: Any) -> InboundEnvelope | None:
        try:
            return decode_inbound(json.loads(record.value.decode("utf-8")))
        except Exception:
            log.error(
                "Skipping inbound record %d of %r: it does not decode, and the next settlement covers it",
                record.offset,
                self._topic,
                exc_info=True,
            )
            return None


class _KafkaOutboxReader(IOutboxReader):
    def __init__(self, *, consumer: Any, partition: TopicPartition, name: str) -> None:
        self._consumer: Any = consumer
        self._partition = partition
        self._name = name
        self._condition = threading.Condition()
        self._closed = False
        self._polling = False
        self._in_flight: tuple[str, PendingDelivery] | None = None
        self._pending: deque[PendingDelivery] = deque()
        self._token = uuid.uuid4().hex
        self._receipts = itertools.count(1)
        self._outage = BrokerOutage(
            logger=log, retry_after_sec=BROKER_RETRY_SEC, report_every_sec=BROKER_REPORT_INTERVAL_SEC
        )

    def poll(self, timeout: float = 0.0) -> DeliveredMessage | None:
        deadline = time.monotonic() + max(0.0, timeout)
        if self._is_closed() or self._outage.stands(deadline):
            return None
        try:
            delivered = self._deliver_next(deadline, floor_ms=KAFKA_ZERO_TIMEOUT_FETCH_MS if timeout <= 0 else 0)
        except _UNANSWERED as error:
            if self._is_closed():
                return None
            self._outage.record(
                deadline,
                "The outbound reader %r of topic %r cannot read (%s); retrying every %.0fs. The transport keeps "
                "consuming and takes what the topic still retains when the broker answers again.",
                self._name,
                self._partition.topic,
                error,
                BROKER_RETRY_SEC,
            )
            return None
        self._outage.cleared("The outbound reader %r of topic %r can read again", self._name, self._partition.topic)
        return delivered

    def ack(self, delivered: DeliveredMessage) -> None:
        with self._condition:
            if self._in_flight is None or self._in_flight[0] != delivered.receipt:
                return
            self._in_flight = None
            self._condition.notify_all()

    def nack(self, delivered: DeliveredMessage) -> None:
        self._retry(delivered, delay=0)

    def defer(self, delivered: DeliveredMessage, *, delay: float) -> None:
        self._retry(delivered, delay=max(0.001, delay))

    def _retry(self, delivered: DeliveredMessage, *, delay: float) -> None:
        with self._condition:
            if self._in_flight is None or self._in_flight[0] != delivered.receipt:
                return
            pending = self._in_flight[1]
            pending.available_at = time.monotonic() + delay
            pending.attempts += int(delay == 0)
            self._pending.appendleft(pending)
            self._in_flight = None
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._in_flight = None
            self._pending.clear()
            self._condition.notify_all()
            if self._polling:
                return
            consumer, self._consumer = self._consumer, None
        _release(consumer, name=self._name, topic=self._partition.topic)

    def _is_closed(self) -> bool:
        with self._condition:
            return self._closed

    def _deliver_next(self, deadline: float, *, floor_ms: int) -> DeliveredMessage | None:
        skipped = 0
        while True:
            consumer = self._enter(deadline)
            if consumer is None:
                return None
            try:
                with self._condition:
                    pending, delay = PendingDelivery.take(self._pending, time.monotonic())
                    if pending is not None:
                        return self._delivery(pending)
                if skipped >= MAX_SKIPPED_RECORDS_PER_POLL:
                    return None
                fetch_deadline = min(deadline, time.monotonic() + delay)
                records = _records(consumer.poll(timeout_ms=kafka_fetch_ms(fetch_deadline, floor_ms), max_records=1))
            finally:
                released = self._leave()
            if released:
                return None
            floor_ms = 0
            for record in records:
                skipped += 1
                message = self._decoded(record)
                if message is not None:
                    with self._condition:
                        if len(self._pending) >= _MAX_PENDING_PER_READER:
                            self._pending.popleft()
                            log.error("Outbox backlog for %r is full; dropped oldest message", self._name)
                        self._pending.append(PendingDelivery(message))
                    continue
            if not records and time.monotonic() >= deadline:
                return None

    def _enter(self, deadline: float) -> Any:
        with self._condition:
            while True:
                if self._closed:
                    return None
                if self._in_flight is None:
                    self._polling = True
                    return self._consumer
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def _leave(self) -> bool:
        with self._condition:
            self._polling = False
            if not self._closed:
                return False
            consumer, self._consumer = self._consumer, None
        if consumer is not None:
            _release(consumer, name=self._name, topic=self._partition.topic)
        return True

    def _decoded(self, record: Any) -> OutboundMessage | None:
        try:
            return decode_outbound(json.loads(record.value.decode("utf-8")))
        except Exception:
            log.error(
                "Skipping outbound record %d of %r for reader %r: it does not decode",
                record.offset,
                self._partition.topic,
                self._name,
                exc_info=True,
            )
            return None

    def _delivery(self, pending: PendingDelivery) -> DeliveredMessage | None:
        with self._condition:
            if self._closed:
                return None
            receipt = f"{self._token}:{next(self._receipts)}"
            self._in_flight = (receipt, pending)
        return DeliveredMessage(pending.message, receipt, pending.attempts)


class KafkaOutbox(IOutbox):
    def __init__(self, *, clients: IKafkaClientFactory, topic: str) -> None:
        self._clients = clients
        self._topic = topic

    def reader(self, *, name: str, mode: SubscriptionMode) -> IOutboxReader:
        transport_name = str(name or "").strip()
        if not transport_name:
            raise ValueError("an outbox reader requires the transport's name")
        requested = SubscriptionMode(mode)
        if requested is not SubscriptionMode.BROADCAST:
            raise ValueError(
                f"subscription {transport_name!r} cannot be opened as {requested.value}: the Kafka backend opens "
                f"{SubscriptionMode.BROADCAST.value} readers only"
            )
        consumer = self._clients.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST)
        partition = TopicPartition(self._topic, _PARTITION)
        try:
            consumer.assign([partition])
            consumer.seek_to_end(partition)
            self._anchor(consumer, partition, transport_name)
        except BaseException:
            _release(consumer, name=transport_name, topic=self._topic)
            raise
        return _KafkaOutboxReader(consumer=consumer, partition=partition, name=transport_name)

    def _anchor(self, consumer: Any, partition: TopicPartition, name: str) -> None:
        deadline = time.monotonic() + _READER_START_TIMEOUT_MS / 1_000
        refusal: Exception | None = None
        while True:
            attempt_ms = min(_READER_START_ATTEMPT_MS, int((deadline - time.monotonic()) * 1_000))
            if attempt_ms <= 0:
                raise KafkaTimeoutError(
                    f"outbound reader {name!r} could not find the end of topic {self._topic!r} within "
                    f"{_READER_START_TIMEOUT_MS} ms "
                    f"({'no position' if refusal is None else type(refusal).__name__})"
                ) from refusal
            attempt_ends = time.monotonic() + attempt_ms / 1_000
            try:
                if consumer.position(partition, timeout_ms=attempt_ms) is not None:
                    return
            except _UNANSWERED as error:
                refusal = error
                time.sleep(max(0.0, attempt_ends - time.monotonic()))

    def publish(self, message: OutboundMessage, *, before_publish: Callable[[], float] | None = None) -> None:
        value = _outbound_value(message)
        if before_publish is None:
            self._clients.producer(topic=self._topic).send(self._topic, value=value, partition=_PARTITION).get(
                timeout=_SEND_TIMEOUT_SEC
            )
            return
        deadline = time.monotonic() + self._authority_left(before_publish)
        producer = self._clients.producer(topic=self._topic)
        producer.partitions_for(self._topic)
        remaining = _unexpired(min(self._authority_left(before_publish), deadline - time.monotonic()))
        producer.send(self._topic, value=value, partition=_PARTITION).get(timeout=min(remaining, _SEND_TIMEOUT_SEC))

    @staticmethod
    def _authority_left(before_publish: Callable[[], float]) -> float:
        return _unexpired(before_publish())


class KafkaMessageGateway(IMessageGateway):
    def __init__(self, *, inbox: KafkaInbox, outbox: KafkaOutbox, clients: IKafkaClientFactory) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._clients = clients
        self._producer: AIOKafkaProducer | None = None

    async def start(self) -> None:
        if self._producer is not None:
            raise RuntimeError("Kafka async producer already started")
        producer = self._clients.async_producer()
        try:
            async with asyncio.timeout(KAFKA_JOIN_TIMEOUT_SEC):
                await producer.start()
        except BaseException:
            try:
                await self._stop(producer)
            except Exception:
                log.warning("Could not close Kafka producer after unsuccessful start", exc_info=True)
            raise
        self._producer = producer

    @property
    def inbox(self) -> KafkaInbox:
        return self._inbox

    @property
    def outbox(self) -> KafkaOutbox:
        return self._outbox

    async def asubmit(self, envelope: InboundEnvelope) -> None:
        await self._send(self._inbox._topic, _inbound_value(envelope))

    async def apublish(self, message: OutboundMessage) -> None:
        await self._send(self._outbox._topic, _outbound_value(message))

    async def _send(self, topic: str, value: bytes) -> None:
        if self._producer is None:
            raise RuntimeError("Kafka async producer is not started")
        async with asyncio.timeout(_SEND_TIMEOUT_SEC):
            await self._producer.send_and_wait(topic, value=value, partition=_PARTITION)

    async def aclose(self) -> None:
        producer, self._producer = self._producer, None
        if producer is not None:
            await self._stop(producer)

    @staticmethod
    async def _stop(producer: AIOKafkaProducer) -> None:
        async def close() -> None:
            pending = asyncio.create_task(producer.stop())
            try:
                await asyncio.wait_for(asyncio.shield(pending), KAFKA_CLOSE_TIMEOUT_MS / 1000)
            except TimeoutError as error:
                # aiokafka.stop cannot be cancelled mid-flush: it sets _closed before closing resources.
                producer._message_accumulator.fail_all(error)
                try:
                    await asyncio.wait_for(pending, KAFKA_JOIN_TIMEOUT_SEC)
                finally:
                    async with asyncio.timeout(KAFKA_CLOSE_TIMEOUT_MS / 1000):
                        await producer.client.close()
                raise

        await finish_file_io(close())


__all__ = [
    "KAFKA_INBOX_MAX_POLL_INTERVAL_MS",
    "MAX_SKIPPED_RECORDS_PER_POLL",
    "KafkaInbox",
    "KafkaMessageGateway",
    "KafkaOutbox",
]
