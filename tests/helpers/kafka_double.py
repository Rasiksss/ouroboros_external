from __future__ import annotations

import random
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import kafka.errors as Errors
from kafka.consumer.fetcher import ConsumerRecord
from kafka.future import Future
from kafka.partitioner.default import murmur2
from kafka.producer.future import RecordMetadata
from kafka.record.default_records import DefaultRecordBatchBuilder
from kafka.structs import OffsetAndMetadata, TopicPartition
from kafka.util import ensure_valid_topic_name

import infra.kafka_client as kafka_client
from infra.kafka_client import IKafkaClientFactory, KafkaOffsetReset, refuse_unusable_poll_interval
from tests.helpers.kafka_double_async import DoubleAsyncGroupConsumer, DoubleAsyncProducer
from tests.helpers.kafka_double_clients import (
    DoubleGroupConsumer,
    DoublePartitionReader,
    DoubleProducer,
    PositionedReader,
    expired,
    wait_slice,
)


@dataclass
class _Record:
    key: bytes | None
    value: bytes | None
    timestamp: int


@dataclass
class _Partition:
    log_start: int = 0
    records: list[_Record | None] = field(default_factory=list)

    @property
    def end(self) -> int:
        return len(self.records)

    def move_log_start(self, offset: int) -> None:
        for position in range(self.log_start, min(offset, self.end)):
            self.records[position] = None
        self.log_start = max(self.log_start, offset)


@dataclass
class _Topic:
    partitions: list[_Partition]
    configs: dict[str, str]
    retained: int | None = None


@dataclass
class _Member:
    consumer: DoubleGroupConsumer
    topics: frozenset[str]
    rejoined: bool
    generation: int | None = None


@dataclass
class _Group:
    committed: dict[TopicPartition, OffsetAndMetadata] = field(default_factory=dict)
    members: dict[str, _Member] = field(default_factory=dict)
    generation: int = 0
    rebalancing: bool = False


@dataclass
class _HeldSend:
    producer: DoubleProducer
    topic: str
    partition: int
    key: bytes | None
    value: bytes | None
    future: SendFuture
    created: float


class SendFuture(Future):
    def __init__(self, *, cluster: KafkaDoubleCluster) -> None:
        super().__init__()
        self._cluster = cluster

    def get(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cluster._changed:
            while not self.is_done:
                self._cluster._expire_held_sends()
                if self.is_done:
                    break
                if expired(deadline):
                    raise Errors.KafkaTimeoutError(f"Timeout after waiting for {timeout} secs.")
                self._cluster._changed.wait(wait_slice(deadline))
        if self.failed():
            raise self.exception
        return self.value


class KafkaDoubleCluster:
    def __init__(self) -> None:
        self._changed = threading.Condition(threading.RLock())
        self._topics: dict[str, _Topic] = {}
        self._groups: dict[str, _Group] = {}
        self._outage_since: float | None = None
        self._rejections: dict[str, BaseException] = {}
        self._held: list[_HeldSend] = []

    def create_topic(self, name: str, *, partitions: int, configs: Mapping[str, str]) -> None:
        ensure_valid_topic_name(name)
        with self._changed:
            if name in self._topics:
                raise Errors.TopicAlreadyExistsError(name)
            self._topics[name] = _Topic([_Partition() for _ in range(partitions)], dict(configs))
            self._changed.notify_all()

    def set_outage(self, active: bool) -> None:
        with self._changed:
            if active:
                self._outage_since = self._outage_since or time.monotonic()
            elif self._outage_since is not None:
                lasted = time.monotonic() - self._outage_since
                self._outage_since = None
                self._evict_sessions_lost_in(lasted)
                self._expire_held_sends()
                held, self._held = self._held, []
                for send in held:
                    self._deliver(send.topic, send.partition, send.key, send.value, send.future)
            self._changed.notify_all()

    def reject_next_send(self, topic: str, error: BaseException | type[BaseException]) -> None:
        with self._changed:
            self._rejections[topic] = error if isinstance(error, BaseException) else error()

    def crash(self, consumer: DoubleGroupConsumer | DoubleAsyncGroupConsumer) -> None:
        if isinstance(consumer, DoubleAsyncGroupConsumer):
            consumer = consumer.wrapped_group_consumer
        if not isinstance(consumer, DoubleGroupConsumer):
            raise TypeError("crash takes the double's own group consumer, not a proxy around it")
        with self._changed:
            group = self._groups.get(consumer._group_id)
            if group is None or consumer._member_id not in group.members:
                raise ValueError("crash takes a consumer that is a member of its group")
            self._remove_member(group, consumer._member_id)
            self._changed.notify_all()

    def append_raw(self, topic: str, partition: int, value: bytes | None, key: bytes | None = None) -> int:
        with self._changed:
            offset = self._append(topic, partition, key, value)
            self._changed.notify_all()
            return offset

    def delete_up_to(self, topic: str, partition: int, offset: int) -> None:
        with self._changed:
            log_partition = self._topics[topic].partitions[partition]
            if offset > log_partition.end:
                raise Errors.OffsetOutOfRangeError({TopicPartition(topic, partition): offset})
            log_partition.move_log_start(offset)
            self._changed.notify_all()

    def retain_last(self, topic: str, count: int) -> None:
        with self._changed:
            self._topics[topic].retained = count
            for log_partition in self._topics[topic].partitions:
                log_partition.move_log_start(log_partition.end - count)
            self._changed.notify_all()

    def discard_positions(self, group_id: str) -> None:
        with self._changed:
            group = self._group(group_id)
            if group.members:
                raise ValueError("a broker discards the positions of a group only while the group has no member")
            group.committed.clear()
            self._changed.notify_all()

    def committed(self, group: str, topic: str, partition: int) -> int | None:
        with self._changed:
            committed = self._groups.get(group, _Group()).committed.get(TopicPartition(topic, partition))
            return None if committed is None else committed.offset

    def end_offset(self, topic: str, partition: int) -> int:
        with self._changed:
            return self._topics[topic].partitions[partition].end

    @property
    def _outage(self) -> bool:
        return self._outage_since is not None

    def _append(self, topic: str, partition: int, key: bytes | None, value: bytes | None) -> int:
        entry = self._topics[topic]
        log_partition = entry.partitions[partition]
        log_partition.records.append(_Record(key, value, int(time.time() * 1000)))
        if entry.retained is not None:
            log_partition.move_log_start(log_partition.end - entry.retained)
        return log_partition.end - 1

    def _deliver(self, topic: str, partition: int, key: bytes | None, value: bytes | None, future: SendFuture) -> None:
        rejection = self._rejections.pop(topic, None)
        limit = self._topics[topic].configs.get("max.message.bytes")
        if rejection is not None:
            future.failure(rejection)
        elif limit is not None and DefaultRecordBatchBuilder.estimate_size_in_bytes(key, value, []) > int(limit):
            future.failure(Errors.MessageSizeTooLargeError(f"a record over max.message.bytes={limit} of {topic}"))
        else:
            offset = self._append(topic, partition, key, value)
            future.success(
                RecordMetadata(
                    topic,
                    partition,
                    TopicPartition(topic, partition),
                    offset,
                    int(time.time() * 1000),
                    None,
                    -1 if key is None else len(key),
                    -1 if value is None else len(value),
                    -1,
                )
            )
        self._changed.notify_all()

    def _send(
        self, producer: DoubleProducer, topic: str, partition: int, key: bytes | None, value: bytes | None
    ) -> SendFuture:
        future = SendFuture(cluster=self)
        with self._changed:
            if self._outage:
                self._held.append(_HeldSend(producer, topic, partition, key, value, future, time.monotonic()))
            else:
                self._deliver(topic, partition, key, value, future)
        return future

    def _expire_held_sends(self) -> None:
        bound = kafka_client._PRODUCER_DELIVERY_TIMEOUT_MS / 1000
        now = time.monotonic()
        for send in [send for send in self._held if now - send.created >= bound]:
            self._held.remove(send)
            send.future.failure(
                Errors.KafkaTimeoutError(
                    f"Expiring 1 record(s) for {send.topic}-{send.partition}: "
                    f"{int((now - send.created) * 1000)} ms has passed since batch creation"
                )
            )
            self._changed.notify_all()

    def _holds_sends_of(self, producer: DoubleProducer) -> bool:
        self._expire_held_sends()
        return any(send.producer is producer for send in self._held)

    def _group(self, group_id: str) -> _Group:
        group = self._groups.setdefault(group_id, _Group())
        now = time.monotonic()
        for member_id, member in list(group.members.items()):
            consumer = member.consumer
            if consumer._closed_at is not None:
                gone = now - consumer._closed_at > kafka_client._GROUP_SESSION_TIMEOUT_MS / 1000
            else:
                gone = now - consumer._last_poll > consumer._max_poll_interval_ms / 1000
            if gone:
                self._remove_member(group, member_id)
        return group

    def _evict_sessions_lost_in(self, lasted: float) -> None:
        if lasted <= kafka_client._GROUP_SESSION_TIMEOUT_MS / 1000:
            return
        for group in self._groups.values():
            for member_id in list(group.members):
                self._remove_member(group, member_id)

    def _remove_member(self, group: _Group, member_id: str) -> None:
        del group.members[member_id]
        if not group.members:
            group.rebalancing = False
            return
        if not group.rebalancing:
            group.rebalancing = True
            for member in group.members.values():
                member.rejoined = False

    def _step(self, consumer: DoubleGroupConsumer) -> None:
        group = self._group(consumer._group_id)
        member = group.members.get(consumer._member_id or "")
        if member is None:
            consumer._forget_assignment()
            consumer._member_id = f"{consumer._client_id}-{uuid.uuid4()}"
            if not group.rebalancing:
                for other in group.members.values():
                    other.rejoined = False
            group.rebalancing = True
            group.members[consumer._member_id] = _Member(consumer, frozenset(consumer._subscription or ()), True)
        elif group.rebalancing and not member.rejoined:
            consumer._forget_assignment()
            member.rejoined = True
        if group.rebalancing and all(member.rejoined for member in group.members.values()):
            self._complete_rebalance(group)

    def _complete_rebalance(self, group: _Group) -> None:
        group.generation += 1
        group.rebalancing = False
        members = [group.members[member_id] for member_id in sorted(group.members)]
        assigned: dict[int, set[TopicPartition]] = {id(member): set() for member in members}
        for topic in sorted(set().union(*(member.topics for member in members))):
            if topic not in self._topics:
                continue
            subscribed = [member for member in members if topic in member.topics]
            share, extra = divmod(len(self._topics[topic].partitions), len(subscribed))
            start = 0
            for index, member in enumerate(subscribed):
                count = share + (1 if index < extra else 0)
                assigned[id(member)].update(
                    TopicPartition(topic, partition) for partition in range(start, start + count)
                )
                start += count
        for member in members:
            member.generation = group.generation
            member.consumer._take_assignment(assigned[id(member)])
        self._changed.notify_all()

    def _await_answer(self, deadline: float | None, timeout_ms: float | None) -> None:
        if timeout_ms == 0:
            raise Errors.KafkaTimeoutError()
        while self._outage:
            if expired(deadline):
                raise Errors.KafkaTimeoutError()
            self._changed.wait(wait_slice(deadline))

    def _commit(self, group_id: str, offsets: Mapping[TopicPartition, OffsetAndMetadata], member_id: str) -> None:
        group = self._group(group_id)
        member = group.members.get(member_id)
        if member is None:
            raise Errors.CommitFailedError(Errors.UnknownMemberIdError())
        if group.rebalancing and member.rejoined:
            raise Errors.RebalanceInProgressError("Offset commit cannot be completed during a rebalance")
        group.committed.update(offsets)

    def _placement(self, topic: str, key: bytes | None) -> int:
        partitions = list(range(len(self._topics[topic].partitions)))
        if key is None:
            return random.choice(partitions)
        return partitions[(murmur2(key) & 0x7FFFFFFF) % len(partitions)]

    def _read(self, reader: PositionedReader, max_records: int) -> dict[TopicPartition, list[ConsumerRecord]]:
        taken: dict[TopicPartition, list[ConsumerRecord]] = {}
        count = 0
        for tp in reader._rotated():
            entry = self._topics.get(tp.topic)
            position = reader._positions.get(tp)
            if entry is None or position is None or count >= max_records:
                continue
            log_partition = entry.partitions[tp.partition]
            if not log_partition.log_start <= position <= log_partition.end:
                position = log_partition.log_start if reader._reset == KafkaOffsetReset.EARLIEST else log_partition.end
            while position < log_partition.end and count < max_records:
                record = log_partition.records[position]
                assert record is not None
                taken.setdefault(tp, []).append(
                    ConsumerRecord(
                        tp.topic,
                        tp.partition,
                        0,
                        position,
                        record.timestamp,
                        0,
                        record.key,
                        record.value,
                        [],
                        None,
                        -1 if record.key is None else len(record.key),
                        -1 if record.value is None else len(record.value),
                        -1,
                    )
                )
                position += 1
                count += 1
            reader._positions[tp] = position
        return taken

    def _resolve(self, reader: PositionedReader, tp: TopicPartition, strategy: KafkaOffsetReset) -> bool:
        entry = self._topics.get(tp.topic)
        if self._outage or entry is None:
            return False
        log_partition = entry.partitions[tp.partition]
        reader._positions[tp] = log_partition.log_start if strategy == KafkaOffsetReset.EARLIEST else log_partition.end
        reader._resets.pop(tp, None)
        return True


class KafkaDoubleClientFactory(IKafkaClientFactory):
    def __init__(self, *, cluster: KafkaDoubleCluster) -> None:
        self._cluster = cluster
        self._producers: dict[str, DoubleProducer] = {}
        self._lock = threading.Lock()

    def producer(self, *, topic: str) -> Any:
        with self._lock:
            return self._producers.setdefault(topic, DoubleProducer(cluster=self._cluster))

    def async_producer(self) -> Any:
        return DoubleAsyncProducer(cluster=self._cluster)

    def group_consumer(
        self, *, group_id: str, client_id: str, auto_offset_reset: KafkaOffsetReset, max_poll_interval_ms: int
    ) -> Any:
        refuse_unusable_poll_interval(max_poll_interval_ms)
        return DoubleGroupConsumer(
            cluster=self._cluster,
            group_id=group_id,
            client_id=client_id,
            reset=auto_offset_reset,
            max_poll_interval_ms=max_poll_interval_ms,
        )

    def async_group_consumer(
        self, *, group_id: str, client_id: str, auto_offset_reset: KafkaOffsetReset, max_poll_interval_ms: int
    ) -> Any:
        refuse_unusable_poll_interval(max_poll_interval_ms)
        return DoubleAsyncGroupConsumer(
            cluster=self._cluster,
            group_id=group_id,
            client_id=client_id,
            reset=auto_offset_reset,
            max_poll_interval_ms=max_poll_interval_ms,
        )

    def partition_reader(self, *, auto_offset_reset: KafkaOffsetReset) -> Any:
        return DoublePartitionReader(cluster=self._cluster, reset=auto_offset_reset)


__all__ = ["KafkaDoubleClientFactory", "KafkaDoubleCluster", "SendFuture"]
