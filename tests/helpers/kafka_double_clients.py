from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import kafka.errors as Errors
from kafka.consumer.fetcher import ConsumerRecord
from kafka.structs import OffsetAndMetadata, TopicPartition
from kafka.util import ensure_valid_topic_name

import infra.kafka_client as kafka_client
from infra.kafka_client import KafkaOffsetReset

if TYPE_CHECKING:
    from tests.helpers.kafka_double import KafkaDoubleCluster

WAIT_SLICE_SEC = 0.02


def deadline_after(timeout_ms: float | None) -> float | None:
    return None if timeout_ms is None else time.monotonic() + timeout_ms / 1000


def expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def wait_slice(deadline: float | None) -> float:
    if deadline is None:
        return WAIT_SLICE_SEC
    return max(0.0, min(WAIT_SLICE_SEC, deadline - time.monotonic()))


def refuse_unsupported(**arguments: Any) -> None:
    for name, value in arguments.items():
        if value:
            raise NotImplementedError(f"the Kafka double does not emulate {name}")


class DoubleProducer:
    def __init__(self, *, cluster: KafkaDoubleCluster) -> None:
        self._cluster = cluster
        self._closed = False
        self._known_topics: set[str] = set()

    def send(self, topic, value=None, key=None, headers=None, partition=None, timestamp_ms=None):
        assert not self._closed, "KafkaProducer already closed!"
        assert not (value is None and key is None), "Need at least one: key or value"
        refuse_unsupported(headers=headers, timestamp_ms=timestamp_ms)
        ensure_valid_topic_name(topic)
        assert type(key) in (bytes, bytearray, memoryview, type(None))
        assert type(value) in (bytes, bytearray, memoryview, type(None))
        partitions = self._wait_on_metadata(topic)
        if partition is not None:
            assert partition >= 0
            assert partition in partitions, "Unrecognized partition"
        else:
            partition = self._cluster._placement(topic, None if key is None else bytes(key))
        return self._cluster._send(
            self, topic, partition, None if key is None else bytes(key), None if value is None else bytes(value)
        )

    def partitions_for(self, topic):
        return self._wait_on_metadata(topic)

    def flush(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cluster._changed:
            while self._cluster._holds_sends_of(self):
                if expired(deadline):
                    raise Errors.KafkaTimeoutError("Timeout waiting for future")
                self._cluster._changed.wait(wait_slice(deadline))

    def close(self, timeout=None, null_logger=False):
        refuse_unsupported(null_logger=null_logger)
        if self._closed:
            return
        assert timeout is None or timeout >= 0
        self.flush(timeout)
        self._closed = True

    def bootstrap_connected(self):
        return False

    def _wait_on_metadata(self, topic: str) -> set[int]:
        max_block_ms = kafka_client._PRODUCER_MAX_BLOCK_MS
        deadline = time.monotonic() + max_block_ms / 1000
        with self._cluster._changed:
            while True:
                entry = self._cluster._topics.get(topic)
                if entry is not None and (topic in self._known_topics or not self._cluster._outage):
                    self._known_topics.add(topic)
                    return set(range(len(entry.partitions)))
                if expired(deadline):
                    raise Errors.KafkaTimeoutError(f"Failed to update metadata after {max_block_ms / 1000:.1f} secs.")
                self._cluster._changed.wait(wait_slice(deadline))


class PositionedReader:
    def __init__(self, *, cluster: KafkaDoubleCluster, reset: KafkaOffsetReset) -> None:
        self._cluster = cluster
        self._reset = KafkaOffsetReset(reset)
        self._closed = False
        self._assignment: set[TopicPartition] = set()
        self._positions: dict[TopicPartition, int | None] = {}
        self._resets: dict[TopicPartition, KafkaOffsetReset] = {}
        self._rotation = 0

    def poll(self, timeout_ms=0, max_records=None, update_offsets=True):
        assert timeout_ms >= 0, "Timeout must not be negative"
        max_records = 1 if max_records is None else max_records
        assert isinstance(max_records, int), "max_records must be an integer"
        assert max_records > 0, "max_records must be positive"
        assert not self._closed, "KafkaConsumer is closed"
        refuse_unsupported(update_offsets=not update_offsets)
        deadline = deadline_after(timeout_ms)
        with self._cluster._changed:
            while not self._closed:
                records = self._poll_once(max_records)
                if records:
                    return records
                if expired(deadline):
                    break
                self._cluster._changed.wait(wait_slice(deadline))
        return {}

    def close(self, autocommit=True, timeout_ms=None):
        with self._cluster._changed:
            self._closed = True
            self._cluster._changed.notify_all()

    def bootstrap_connected(self):
        return False

    def _poll_once(self, max_records: int) -> dict[TopicPartition, list[ConsumerRecord]]:
        if self._cluster._outage:
            return {}
        for tp in sorted(self._assignment):
            if self._positions.get(tp) is None:
                self._cluster._resolve(self, tp, self._resets.get(tp, self._reset))
            elif tp in self._resets:
                self._cluster._resolve(self, tp, self._resets[tp])
        return self._cluster._read(self, max_records)

    def _rotated(self) -> list[TopicPartition]:
        ordered = sorted(self._assignment)
        if not ordered:
            return []
        self._rotation = (self._rotation + 1) % len(ordered)
        return ordered[self._rotation :] + ordered[: self._rotation]


class DoublePartitionReader(PositionedReader):
    def assign(self, partitions):
        with self._cluster._changed:
            self._assignment = set(partitions)
            self._positions = {tp: self._positions.get(tp) for tp in self._assignment}
            self._resets = {tp: strategy for tp, strategy in self._resets.items() if tp in self._assignment}

    def seek(self, partition, offset):
        if not isinstance(partition, TopicPartition):
            raise TypeError("partition must be a TopicPartition namedtuple")
        assert isinstance(offset, int) and offset >= 0, "Offset must be >= 0"
        assert partition in self._assignment, "Unassigned partition"
        with self._cluster._changed:
            self._positions[partition] = offset
            self._resets.pop(partition, None)

    def seek_to_beginning(self, *partitions):
        self._request_reset(partitions, KafkaOffsetReset.EARLIEST)

    def seek_to_end(self, *partitions):
        self._request_reset(partitions, KafkaOffsetReset.LATEST)

    def position(self, partition, timeout_ms=None):
        if not isinstance(partition, TopicPartition):
            raise TypeError("partition must be a TopicPartition namedtuple")
        assert partition in self._assignment, "Partition is not assigned"
        deadline = deadline_after(timeout_ms)
        with self._cluster._changed:
            while True:
                if self._positions.get(partition) is not None and partition not in self._resets:
                    return self._positions[partition]
                if self._cluster._resolve(self, partition, self._resets.get(partition, self._reset)):
                    return self._positions[partition]
                if expired(deadline):
                    return None
                self._cluster._changed.wait(wait_slice(deadline))

    def _request_reset(self, partitions: tuple[TopicPartition, ...], strategy: KafkaOffsetReset) -> None:
        if not all(isinstance(tp, TopicPartition) for tp in partitions):
            raise TypeError("partitions must be TopicPartition namedtuples")
        if not partitions:
            partitions = tuple(self._assignment)
            assert partitions, "No partitions are currently assigned"
        for tp in partitions:
            assert tp in self._assignment, "Unassigned partition"
        with self._cluster._changed:
            for tp in partitions:
                self._resets[tp] = strategy


class DoubleGroupConsumer(PositionedReader):
    def __init__(
        self,
        *,
        cluster: KafkaDoubleCluster,
        group_id: str,
        client_id: str,
        reset: KafkaOffsetReset,
        max_poll_interval_ms: int,
    ) -> None:
        super().__init__(cluster=cluster, reset=reset)
        self._group_id = group_id
        self._client_id = client_id
        self._max_poll_interval_ms = max_poll_interval_ms
        self._subscription: set[str] | None = None
        self._member_id: str | None = None
        self._last_poll = time.monotonic()
        self._closed_at: float | None = None

    @property
    def client_id(self) -> str:
        return self._client_id

    def subscribe(self, topics=(), pattern=None, listener=None):
        refuse_unsupported(pattern=pattern, listener=listener)
        with self._cluster._changed:
            self._subscription = set(topics)

    def subscription(self):
        return None if self._subscription is None else set(self._subscription)

    def assignment(self):
        return set(self._assignment)

    def commit(self, offsets=None, timeout_ms=None):
        if offsets is None:
            offsets = {
                tp: OffsetAndMetadata(position, "", -1)
                for tp, position in self._positions.items()
                if position is not None
            }
        assert all(isinstance(tp, TopicPartition) for tp in offsets)
        assert all(isinstance(offset, OffsetAndMetadata) for offset in offsets.values())
        if not offsets:
            return
        deadline = deadline_after(timeout_ms)
        with self._cluster._changed:
            if self._closed:
                while not expired(deadline):
                    self._cluster._changed.wait(wait_slice(deadline))
                raise Errors.KafkaTimeoutError()
            self._cluster._await_answer(deadline, timeout_ms)
            if self._member_id is None:
                raise Errors.CommitFailedError(
                    "Offset commit cannot be completed since the consumer is not part of an active group"
                )
            self._cluster._commit(self._group_id, offsets, self._member_id)
            self._cluster._changed.notify_all()

    def close(self, autocommit=True, timeout_ms=None):
        deadline = deadline_after(timeout_ms)
        with self._cluster._changed:
            if self._closed:
                return
            self._closed = True
            self._closed_at = time.monotonic()
            group = self._cluster._groups.get(self._group_id)
            joined = group is not None and self._member_id in group.members
            while joined and self._cluster._outage and not expired(deadline):
                self._cluster._changed.wait(wait_slice(deadline))
            if joined and not self._cluster._outage and group is not None and self._member_id in group.members:
                self._cluster._remove_member(group, self._member_id)
            self._assignment = set()
            self._cluster._changed.notify_all()

    def _poll_once(self, max_records: int) -> dict[TopicPartition, list[ConsumerRecord]]:
        self._last_poll = time.monotonic()
        if self._cluster._outage or self._subscription is None:
            return {}
        self._cluster._step(self)
        if not self._assignment:
            return {}
        group = self._cluster._groups[self._group_id]
        for tp in sorted(self._assignment):
            if self._positions.get(tp) is None and tp in group.committed:
                self._positions[tp] = group.committed[tp].offset
        return super()._poll_once(max_records)

    def _forget_assignment(self) -> None:
        self._assignment = set()
        self._positions = {}
        self._resets = {}

    def _take_assignment(self, partitions: set[TopicPartition]) -> None:
        self._assignment = set(partitions)
        self._positions = dict.fromkeys(partitions)
        self._resets = {}


__all__ = [
    "WAIT_SLICE_SEC",
    "DoubleGroupConsumer",
    "DoublePartitionReader",
    "DoubleProducer",
    "PositionedReader",
    "deadline_after",
    "expired",
    "refuse_unsupported",
    "wait_slice",
]
