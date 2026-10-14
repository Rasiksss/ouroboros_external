from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from kafka.errors import KafkaTimeoutError
from kafka.structs import TopicPartition

from infra.kafka_client import (
    KAFKA_SEND_TIMEOUT_SEC,
    KAFKA_ZERO_TIMEOUT_FETCH_MS,
    IKafkaClientFactory,
    KafkaOffsetReset,
    close_kafka_consumer,
    kafka_fetch_ms,
    kafka_unanswered_errors,
)
from new_core.coordination.interfaces import ITaskStopConsumer, ITaskStopPublisher, TaskStopNotice
from ouroboros.app.task_stop_notifications import decode_task_stop_notice, encode_task_stop_fields
from ouroboros.contracts.stream_replies import BROKER_REPORT_INTERVAL_SEC, BROKER_RETRY_SEC, BrokerOutage

if TYPE_CHECKING:
    from kafka import KafkaConsumer

log = logging.getLogger(__name__)

MAX_SKIPPED_RECORDS_PER_POLL = 32

_PARTITION = 0
_ANCHOR_TIMEOUT_MS = 5_000
_UNANSWERED = kafka_unanswered_errors()


class KafkaTaskStopPublisher(ITaskStopPublisher):
    def __init__(self, *, clients: IKafkaClientFactory, topic: str, wait_sec: float = KAFKA_SEND_TIMEOUT_SEC) -> None:
        self._clients = clients
        self._topic = topic
        self._wait_sec = float(wait_sec)

    def publish(self, notice: TaskStopNotice) -> None:
        value = json.dumps(encode_task_stop_fields(notice), ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self._clients.producer(topic=self._topic).send(self._topic, value=value, partition=_PARTITION).get(
            timeout=self._wait_sec
        )


class KafkaTaskStopConsumer(ITaskStopConsumer):
    """Read the topic outside any group, so every runtime sees every hint."""

    def __init__(self, *, clients: IKafkaClientFactory, topic: str) -> None:
        self._clients = clients
        self._topic = topic
        self._partition = TopicPartition(topic, _PARTITION)
        self._consumer: KafkaConsumer | None = None
        self._anchor_at_end = True
        self._outage = BrokerOutage(
            logger=log, retry_after_sec=BROKER_RETRY_SEC, report_every_sec=BROKER_REPORT_INTERVAL_SEC
        )

    def start(self) -> bool:
        """Anchor at the end of the topic before this runtime may hold an addressed task."""
        try:
            self._reader()
        except _UNANSWERED:
            self._anchor_at_end = False
            log.warning(
                "The task-stop topic %r could not be anchored; the next poll resumes at the oldest record it "
                "retains, so a hint published meanwhile is replayed rather than missed",
                self._topic,
                exc_info=True,
            )
            return False
        return True

    def poll(self, timeout: float = 0.0) -> TaskStopNotice | None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        if self._outage.stands(deadline):
            return None
        try:
            notice = self._drain(self._reader(), deadline, KAFKA_ZERO_TIMEOUT_FETCH_MS if timeout <= 0 else 0)
        except _UNANSWERED as error:
            self._outage.record(
                deadline,
                "The task-stop topic %r cannot be read (%s); retrying every %.0fs. Stopping still settles in the "
                "task store, so a run addressed while this stands ends on its own next check.",
                self._topic,
                type(error).__name__,
                BROKER_RETRY_SEC,
            )
            return None
        self._outage.cleared("The task-stop topic %r can be read again", self._topic)
        return notice

    def _reader(self) -> KafkaConsumer:
        if self._consumer is not None:
            return self._consumer
        consumer = self._clients.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST)
        try:
            consumer.assign([self._partition])
            if self._anchor_at_end:
                consumer.seek_to_end(self._partition)
            else:
                consumer.seek_to_beginning(self._partition)
            if consumer.position(self._partition, timeout_ms=_ANCHOR_TIMEOUT_MS) is None:
                raise KafkaTimeoutError(
                    f"the task-stop topic {self._topic!r} did not answer its position within {_ANCHOR_TIMEOUT_MS} ms"
                )
        except BaseException:
            failure = close_kafka_consumer(consumer)
            if failure is not None:
                log.warning("Could not close the task-stop reader of %r", self._topic, exc_info=failure)
            raise
        self._anchor_at_end = True
        self._consumer = consumer
        return consumer

    def _drain(self, consumer: KafkaConsumer, deadline: float, floor_ms: int) -> TaskStopNotice | None:
        skipped = 0
        while True:
            fetched = consumer.poll(timeout_ms=kafka_fetch_ms(deadline, floor_ms), max_records=1)
            floor_ms = 0
            records = [record for batch in fetched.values() for record in batch]
            for record in records:
                notice = self._decoded(record)
                if notice is not None:
                    return notice
                skipped += 1
                if skipped >= MAX_SKIPPED_RECORDS_PER_POLL:
                    return None
            if not records and time.monotonic() >= deadline:
                return None

    def _decoded(self, record: Any) -> TaskStopNotice | None:
        try:
            fields = json.loads(record.value.decode("utf-8"))
            if not isinstance(fields, dict):
                raise TypeError(f"the record's value is JSON {type(fields).__name__}, not an object")
        except (AttributeError, TypeError, ValueError):
            log.warning(
                "Skipping task-stop record %d of %r: its value does not decode",
                record.offset,
                self._topic,
                exc_info=True,
            )
            return None
        notice = decode_task_stop_notice(fields)
        if notice is None:
            log.warning(
                "Skipping task-stop record %d of %r: its fields name no hint this build knows",
                record.offset,
                self._topic,
            )
        return notice


__all__ = [
    "MAX_SKIPPED_RECORDS_PER_POLL",
    "KafkaTaskStopConsumer",
    "KafkaTaskStopPublisher",
]
