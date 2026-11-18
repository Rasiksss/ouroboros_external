from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from kafka import TopicPartition
from kafka.errors import CommitFailedError, KafkaError, RebalanceInProgressError
from kafka.structs import OffsetAndMetadata

from infra.kafka_client import (
    KAFKA_SEND_TIMEOUT_SEC,
    KAFKA_ZERO_TIMEOUT_FETCH_MS,
    IKafkaClientFactory,
    KafkaOffsetReset,
    kafka_fetch_ms,
    kafka_group_member,
    kafka_unanswered_errors,
)
from new_core.identity import OwnerScope
from ouroboros.agent_events.codec import (
    UnknownAgentEventKind,
    decode_agent_event,
    encode_agent_event,
    ensure_wire_safe,
)
from ouroboros.agent_events.interfaces import AgentEvent, IAgentEventConsumer, IAgentEventPublisher
from ouroboros.contracts.stream_replies import BROKER_REPORT_INTERVAL_SEC, BROKER_RETRY_SEC, BrokerOutage

if TYPE_CHECKING:
    from kafka import KafkaConsumer
    from kafka.consumer.fetcher import ConsumerRecord

log = logging.getLogger(__name__)

KAFKA_AGENT_EVENT_MAX_POLL_INTERVAL_MS = 300_000
MAX_SKIPPED_RECORDS_PER_POLL = 32

_SEND_TIMEOUT_SEC = KAFKA_SEND_TIMEOUT_SEC
_OFFSET_REQUEST_TIMEOUT_MS = 1_000
_UNANSWERED = kafka_unanswered_errors()


class KafkaAgentEventPublisher(IAgentEventPublisher):
    def __init__(self, *, clients: IKafkaClientFactory, topic: str) -> None:
        self._clients = clients
        self._topic = topic

    def publish(self, event: AgentEvent) -> None:
        if not isinstance(event, AgentEvent):
            raise TypeError(f"the agent-event transport takes an AgentEvent, got {type(event).__name__}")
        ensure_wire_safe(event)
        value = json.dumps(encode_agent_event(event)).encode("utf-8")
        self._clients.producer(topic=self._topic).send(self._topic, key=self.record_key(event.scope), value=value).get(
            timeout=_SEND_TIMEOUT_SEC
        )

    @staticmethod
    def record_key(scope: OwnerScope) -> bytes:
        return json.dumps(
            [scope.owner_id, scope.agent_id, scope.surface_id.value, scope.chat_id],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")

    @staticmethod
    def partition_key(event: AgentEvent) -> OwnerScope:
        return event.scope


class KafkaAgentEventConsumer(IAgentEventConsumer):
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
        self._consumer: KafkaConsumer | None = None
        self._pending: dict[TopicPartition, int] = {}
        self._outage = BrokerOutage(
            logger=log, retry_after_sec=BROKER_RETRY_SEC, report_every_sec=BROKER_REPORT_INTERVAL_SEC
        )

    def poll(self, timeout: float = 0.0) -> AgentEvent | None:
        deadline = time.monotonic() + max(0.0, timeout)
        if self._outage.stands(deadline):
            return None
        first_fetch_ms = kafka_fetch_ms(deadline, KAFKA_ZERO_TIMEOUT_FETCH_MS if timeout <= 0 else 0)
        try:
            event = self._drain(deadline, first_fetch_ms)
        except _UNANSWERED as error:
            self._outage.record(
                deadline,
                "The agent-event topic %r cannot be read (%s); retrying every %.0fs. The tick keeps running and "
                "the reports the topic holds are delivered when it answers again.",
                self._topic,
                type(error).__name__,
                BROKER_RETRY_SEC,
            )
            return None
        if not self._pending:
            self._outage.cleared("The agent-event topic %r can be read again", self._topic)
        return event

    @staticmethod
    def partition_key(event: AgentEvent) -> OwnerScope:
        return event.scope

    def _drain(self, deadline: float, fetch_ms: int) -> AgentEvent | None:
        consumer = self._group_consumer()
        if self._pending and not self._settled_pending(consumer, deadline):
            return None
        skipped = 0
        while True:
            record = self._first_record(consumer.poll(timeout_ms=fetch_ms, max_records=1))
            fetch_ms = kafka_fetch_ms(deadline)
            if record is None:
                if time.monotonic() >= deadline:
                    return None
                continue
            event = self._taken(consumer, record)
            if event is not None:
                return event
            skipped += 1
            if self._pending or skipped >= MAX_SKIPPED_RECORDS_PER_POLL:
                return None

    def _group_consumer(self) -> KafkaConsumer:
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

    def _settled_pending(self, consumer: KafkaConsumer, deadline: float) -> bool:
        pending = dict(self._pending)
        try:
            consumer.commit(
                {partition: OffsetAndMetadata(offset, "", -1) for partition, offset in pending.items()},
                timeout_ms=_OFFSET_REQUEST_TIMEOUT_MS,
            )
        except (CommitFailedError, RebalanceInProgressError) as error:
            self._pending.clear()
            log.warning(
                "The agent-event group %r could not settle %s of %r (%s): its membership has ended, so the last "
                "report taken from each of those partitions may be delivered again once it is reassigned",
                self._group_id,
                ", ".join(
                    f"partition {partition.partition} at offset {offset}" for partition, offset in pending.items()
                ),
                self._topic,
                type(error).__name__,
            )
            return True
        except KafkaError as error:
            self._outage.record(
                deadline,
                "The agent-event topic %r still cannot settle the reports already handed over (%s); nothing more is "
                "taken until it can, retrying every %.0fs",
                self._topic,
                type(error).__name__,
                BROKER_RETRY_SEC,
            )
            return False
        self._pending.clear()
        self._outage.cleared("The agent-event topic %r can be read again", self._topic)
        return True

    def _taken(self, consumer: KafkaConsumer, record: ConsumerRecord) -> AgentEvent | None:
        partition = TopicPartition(record.topic, record.partition)
        try:
            consumer.commit(
                {partition: OffsetAndMetadata(record.offset + 1, "", -1)}, timeout_ms=_OFFSET_REQUEST_TIMEOUT_MS
            )
        except _UNANSWERED as error:
            self._pending[partition] = record.offset + 1
            self._outage.record(
                time.monotonic(),
                "The agent-event topic %r could not settle record %d of partition %d as it was handed over "
                "(%s); the report is delivered and its settlement is repeated before anything more is taken, "
                "retrying every %.0fs",
                self._topic,
                record.offset,
                record.partition,
                type(error).__name__,
                BROKER_RETRY_SEC,
            )
        else:
            self._outage.cleared("The agent-event topic %r can be read again", self._topic)
        return self._decoded(record)

    def _decoded(self, record: ConsumerRecord) -> AgentEvent | None:
        try:
            if record.value is None:
                raise TypeError("the record carries no value")
            entry = json.loads(record.value.decode("utf-8"))
            if not isinstance(entry, dict):
                raise TypeError(f"the record's value is JSON {type(entry).__name__}, not an object")
            return decode_agent_event(entry)
        except UnknownAgentEventKind as unknown:
            log.warning(
                "Skipping agent-event record %d of partition %d of %r: this build does not know the kind %r",
                record.offset,
                record.partition,
                self._topic,
                str(unknown),
            )
            return None
        except (TypeError, ValueError):
            log.error(
                "Dropping agent-event record %d of partition %d of %r: its value does not decode",
                record.offset,
                record.partition,
                self._topic,
                exc_info=True,
            )
            return None

    @staticmethod
    def _first_record(fetched: dict[TopicPartition, list[ConsumerRecord]]) -> ConsumerRecord | None:
        for records in fetched.values():
            for record in records:
                return record
        return None


__all__ = [
    "KAFKA_AGENT_EVENT_MAX_POLL_INTERVAL_MS",
    "MAX_SKIPPED_RECORDS_PER_POLL",
    "KafkaAgentEventConsumer",
    "KafkaAgentEventPublisher",
]
