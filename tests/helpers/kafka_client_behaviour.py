from __future__ import annotations

import inspect
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import KafkaTimeoutError, MessageSizeTooLargeError
from kafka.partitioner.default import murmur2
from kafka.producer.future import FutureRecordMetadata
from kafka.structs import TopicPartition

from infra.kafka_client import IKafkaClientFactory, KafkaOffsetReset
from tests.helpers.kafka_recording import KafkaRecordingClientFactory

JOIN_TIMEOUT_SEC = 20.0
ANSWER_TIMEOUT_MS = 10_000
SHORT_BOUND_MS = 300
PRODUCER_SURFACE = ("send", "partitions_for", "flush", "close", "bootstrap_connected")
GROUP_CONSUMER_SURFACE = ("subscribe", "poll", "commit", "assignment", "subscription", "close", "bootstrap_connected")
PARTITION_READER_SURFACE = (
    "assign",
    "seek",
    "seek_to_beginning",
    "seek_to_end",
    "position",
    "poll",
    "close",
    "bootstrap_connected",
)


def elapsed(action: Callable[[], Any]) -> float:
    started = time.monotonic()
    action()
    return time.monotonic() - started


def raises_within(error: type[BaseException], action: Callable[[], Any], *, at_least: float, at_most: float) -> None:
    started = time.monotonic()
    with pytest.raises(error):
        action()
    assert at_least <= time.monotonic() - started <= at_most


class KafkaClientBehaviour:
    @staticmethod
    def _signature(member: Any) -> list[tuple[str, Any, Any]]:
        return [(p.name, p.default, p.kind) for p in inspect.signature(member).parameters.values()]

    def clients(self) -> IKafkaClientFactory:
        raise NotImplementedError("a behaviour subclass declares the clients under test")

    def create_topic(self, name: str, *, partitions: int) -> None:
        raise NotImplementedError("a behaviour subclass declares how a topic is created")

    def unreachable_clients(self) -> IKafkaClientFactory:
        raise NotImplementedError("a behaviour subclass declares clients whose broker cannot be reached")

    def supports_healing(self) -> bool:
        raise NotImplementedError("a behaviour subclass declares whether an outage can be healed")

    def set_outage(self, active: bool) -> None:
        raise NotImplementedError("a behaviour subclass that heals declares how an outage starts and ends")

    def refuses_topic_auto_creation(self) -> bool:
        raise NotImplementedError("a behaviour subclass declares whether its cluster never creates a topic on demand")

    def crash(self, consumer: Any) -> None:
        raise NotImplementedError("a behaviour subclass declares how a member is dropped")

    def drop_membership(self, consumer: Any) -> None:
        raise NotImplementedError("a behaviour subclass declares how a member loses its membership and stays usable")

    def supports_retention_bound(self) -> bool:
        raise NotImplementedError("a behaviour subclass declares whether a standing retention bound exists")

    def retain_last(self, topic: str, count: int) -> None:
        raise NotImplementedError("a behaviour subclass with a retention bound declares how it is set")

    def delete_up_to(self, topic: str, partition: int, offset: int) -> None:
        raise NotImplementedError("a behaviour subclass declares how records are deleted")

    def size_limited_clients(self, topic: str, *, max_message_bytes: int) -> IKafkaClientFactory:
        raise NotImplementedError("a behaviour subclass declares where the message-size limit is enforced")

    def topic_prefix(self) -> str:
        raise NotImplementedError("a behaviour subclass declares the prefix its topics are named under")

    def _decline_unless(self, capability: Callable[[], bool]) -> None:
        if not capability():
            pytest.skip(f"{type(self).__name__} declares {capability.__name__}() False")

    def _name(self, kind: str) -> str:
        return f"{self.topic_prefix()}.{kind}-{uuid.uuid4().hex[:12]}"

    def _topic(self, *, partitions: int) -> str:
        name = self._name("topic")
        self.create_topic(name, partitions=partitions)
        return name

    def _member(self, clients: IKafkaClientFactory, group: str, topic: str, **overrides: Any) -> Any:
        kwargs = {
            "group_id": group,
            "client_id": f"member-{uuid.uuid4().hex[:6]}",
            "auto_offset_reset": KafkaOffsetReset.EARLIEST,
            "max_poll_interval_ms": 300_000,
            **overrides,
        }
        consumer = clients.group_consumer(**kwargs)
        consumer.subscribe([topic])
        return consumer

    def _poll_until(self, polled: list[Any], done: Callable[[], bool]) -> None:
        deadline = time.monotonic() + JOIN_TIMEOUT_SEC
        while not done():
            assert time.monotonic() < deadline, "the group did not settle in time"
            for consumer in polled:
                consumer.poll(timeout_ms=100)

    def _produce(self, clients: IKafkaClientFactory, topic: str, count: int, *, partition: int = 0) -> None:
        producer = clients.producer(topic=topic)
        for index in range(count):
            producer.send(topic, value=f"record-{index}".encode(), partition=partition).get(timeout=10)

    def _offsets_polled(self, reader: Any, tp: TopicPartition) -> list[int]:
        return [record.offset for record in reader.poll(timeout_ms=ANSWER_TIMEOUT_MS).get(tp, [])]

    def test_a_position_lookup_answers_none_during_an_outage_and_asserts_on_an_unassigned_partition(self):
        clients, tp = self.unreachable_clients(), TopicPartition("fleet.unreachable", 0)
        reader = clients.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST)
        reader.assign([tp])

        started = time.monotonic()
        assert reader.position(tp, timeout_ms=SHORT_BOUND_MS) is None
        assert time.monotonic() - started >= 0.25
        with pytest.raises(AssertionError, match="Partition is not assigned"):
            reader.position(TopicPartition("fleet.unreachable", 1), timeout_ms=SHORT_BOUND_MS)
        reader.close(autocommit=False, timeout_ms=SHORT_BOUND_MS)

    def test_a_timed_out_send_lands_once_the_outage_heals(self):
        self._decline_unless(self.supports_healing)
        clients, topic = self.clients(), self._topic(partitions=1)
        producer = clients.producer(topic=topic)
        producer.send(topic, value=b"warm").get(timeout=10)
        self.set_outage(True)

        held = producer.send(topic, value=b"held")
        with pytest.raises(KafkaTimeoutError):
            held.get(timeout=0.2)
        self.set_outage(False)

        assert held.get(timeout=10).offset == 1

    def test_a_send_to_a_missing_topic_waits_out_the_metadata_bound_and_creates_nothing(self, monkeypatch):
        self._decline_unless(self.refuses_topic_auto_creation)
        monkeypatch.setattr("infra.kafka_client._PRODUCER_MAX_BLOCK_MS", SHORT_BOUND_MS)
        clients, missing = self.clients(), self._name("missing")
        producer = clients.producer(topic=missing)

        raises_within(KafkaTimeoutError, lambda: producer.send(missing, value=b"x"), at_least=0.25, at_most=3.0)
        raises_within(KafkaTimeoutError, lambda: producer.partitions_for(missing), at_least=0.25, at_most=3.0)

    def test_a_send_on_a_closed_producer_asserts(self):
        clients, topic = self.clients(), self._topic(partitions=1)
        producer = clients.producer(topic=topic)
        producer.close(timeout=0)

        with pytest.raises(AssertionError, match="KafkaProducer already closed!"):
            producer.send(topic, value=b"late")

    def test_an_oversize_send_lands_nothing_and_a_send_under_the_ceiling_arrives_whole(self):
        topic = self._name("small")
        tp = TopicPartition(topic, 0)
        clients = self.size_limited_clients(topic, max_message_bytes=1_024)
        producer = clients.producer(topic=topic)
        under = bytes(range(256)) * 2

        with pytest.raises(MessageSizeTooLargeError):
            producer.send(topic, value=b"x" * 4_096).get(timeout=10)
        assert producer.send(topic, value=under).get(timeout=10).offset == 0
        reader = clients.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST)
        reader.assign([tp])
        assert [record.value for record in reader.poll(timeout_ms=ANSWER_TIMEOUT_MS)[tp]] == [under]
        reader.close(autocommit=False, timeout_ms=SHORT_BOUND_MS)

    def test_seeks_and_positions_on_an_unassigned_partition_and_a_poll_after_close_assert(self):
        clients, topic = self.clients(), self._topic(partitions=1)
        tp = TopicPartition(topic, 0)
        reader = clients.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST)

        for seek in (lambda: reader.seek(tp, 0), lambda: reader.seek_to_end(tp), lambda: reader.seek_to_beginning(tp)):
            with pytest.raises(AssertionError, match="Unassigned partition"):
                seek()
        with pytest.raises(AssertionError, match="Partition is not assigned"):
            reader.position(tp, timeout_ms=SHORT_BOUND_MS)
        reader.close(autocommit=False, timeout_ms=SHORT_BOUND_MS)
        with pytest.raises(AssertionError, match="KafkaConsumer is closed"):
            reader.poll(timeout_ms=0)

    def test_seeking_to_the_beginning_reads_the_oldest_record_the_log_still_retains(self):
        clients, topic = self.clients(), self._topic(partitions=1)
        tp = TopicPartition(topic, 0)
        self._produce(clients, topic, 3)
        reader = clients.partition_reader(auto_offset_reset=KafkaOffsetReset.LATEST)
        reader.assign([tp])
        reader.seek_to_end(tp)
        assert reader.position(tp, timeout_ms=ANSWER_TIMEOUT_MS) == 3
        reader.seek_to_beginning(tp)

        assert reader.position(tp, timeout_ms=ANSWER_TIMEOUT_MS) == 0
        assert self._offsets_polled(reader, tp) == [0]
        reader.close(autocommit=False, timeout_ms=SHORT_BOUND_MS)

    def test_an_earliest_reset_resumes_a_position_past_the_log_end_at_the_log_start(self):
        clients, topic = self.clients(), self._topic(partitions=1)
        tp = TopicPartition(topic, 0)
        self._produce(clients, topic, 5)
        reader = clients.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST)
        reader.assign([tp])
        reader.seek(tp, 100)

        assert self._offsets_polled(reader, tp) == [0]
        reader.close(autocommit=False, timeout_ms=SHORT_BOUND_MS)

    def test_max_records_caps_a_poll_and_the_position_moves_past_what_was_returned(self):
        clients, topic = self.clients(), self._topic(partitions=1)
        tp = TopicPartition(topic, 0)
        self._produce(clients, topic, 5)
        reader = clients.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST)
        reader.assign([tp])

        first = reader.poll(timeout_ms=ANSWER_TIMEOUT_MS, max_records=2)
        assert [record.offset for record in first[tp]] == [0, 1]
        assert reader.position(tp, timeout_ms=ANSWER_TIMEOUT_MS) == 2
        second = reader.poll(timeout_ms=ANSWER_TIMEOUT_MS)
        assert [record.offset for record in second[tp]] == [2]
        assert reader.position(tp, timeout_ms=ANSWER_TIMEOUT_MS) == 3
        reader.close(autocommit=False, timeout_ms=SHORT_BOUND_MS)

    def test_keys_place_records_with_the_librarys_partitioner_and_a_named_partition_must_exist(self):
        clients, topic = self.clients(), self._topic(partitions=4)
        producer = clients.producer(topic=topic)

        keyed = producer.send(topic, value=b"v", key=b"owner-a").get(timeout=10)
        named = producer.send(topic, value=b"v", partition=2).get(timeout=10)

        assert keyed.partition == (murmur2(b"owner-a") & 0x7FFFFFFF) % 4
        assert named.partition == 2
        with pytest.raises(AssertionError, match="Unrecognized partition"):
            producer.send(topic, value=b"v", partition=9)

    def test_a_standing_retention_bound_moves_the_log_start_under_an_earliest_reader_on_every_append(self):
        self._decline_unless(self.supports_retention_bound)
        clients, topic = self.clients(), self._topic(partitions=1)
        tp = TopicPartition(topic, 0)
        self.retain_last(topic, 3)
        reader = clients.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST)
        reader.assign([tp])
        reader.seek(tp, 0)

        self._produce(clients, topic, 5)

        assert self._offsets_polled(reader, tp) == [2]
        reader.close(autocommit=False, timeout_ms=SHORT_BOUND_MS)

    def test_a_log_start_moved_under_a_positioned_earliest_reader_resumes_there(self):
        clients, topic = self.clients(), self._topic(partitions=1)
        tp = TopicPartition(topic, 0)
        self._produce(clients, topic, 5)
        reader = clients.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST)
        reader.assign([tp])
        reader.seek(tp, 0)
        assert reader.position(tp, timeout_ms=ANSWER_TIMEOUT_MS) == 0

        self.delete_up_to(topic, 0, 3)

        assert self._offsets_polled(reader, tp) == [3]
        reader.close(autocommit=False, timeout_ms=SHORT_BOUND_MS)

    def test_bootstrap_connected_reads_false_on_a_healthy_cluster(self):
        clients, topic = self.clients(), self._topic(partitions=1)
        producer = clients.producer(topic=topic)
        producer.send(topic, value=b"v").get(timeout=10)
        reader = clients.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST)
        reader.assign([TopicPartition(topic, 0)])
        reader.poll(timeout_ms=ANSWER_TIMEOUT_MS)

        assert producer.bootstrap_connected() is False
        assert reader.bootstrap_connected() is False
        reader.close(autocommit=False, timeout_ms=SHORT_BOUND_MS)

    def test_the_factory_keeps_one_producer_per_topic(self):
        clients, topic, other = self.clients(), self._topic(partitions=1), self._topic(partitions=1)
        producer = clients.producer(topic=topic)
        producer.send(topic, value=b"v").get(timeout=10)

        assert clients.producer(topic=topic) is producer
        assert clients.producer(topic=other) is not producer

    def test_every_emulated_call_carries_the_librarys_signature(self):
        clients, topic, group = self.clients(), self._topic(partitions=1), self._name("group")
        producer = clients.producer(topic=topic)
        future = producer.send(topic, value=b"v")
        future.get(timeout=10)
        built = [
            (producer, KafkaProducer, PRODUCER_SURFACE),
            (
                clients.group_consumer(
                    group_id=group,
                    client_id="sig-0",
                    auto_offset_reset=KafkaOffsetReset.EARLIEST,
                    max_poll_interval_ms=300_000,
                ),
                KafkaConsumer,
                GROUP_CONSUMER_SURFACE,
            ),
            (
                clients.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST),
                KafkaConsumer,
                PARTITION_READER_SURFACE,
            ),
        ]

        mismatched = [
            (type(client).__name__, name)
            for client, library, surface in built
            for name in surface
            if self._signature(getattr(type(client), name)) != self._signature(getattr(library, name))
        ]
        assert mismatched == []
        assert self._signature(type(future).get) == self._signature(FutureRecordMetadata.get)

    def test_a_recording_factory_records_every_client_and_fails_one_call_on_demand(self):
        clients, topic, group = self.clients(), self._topic(partitions=1), self._name("group")
        recording = KafkaRecordingClientFactory(clients=clients)

        consumer = recording.group_consumer(
            group_id=group,
            client_id="recorded-0",
            auto_offset_reset=KafkaOffsetReset.EARLIEST,
            max_poll_interval_ms=300_000,
        )
        consumer.subscribe([topic])
        producer = recording.producer(topic=topic)
        producer.fail_next("send", KafkaTimeoutError())

        with pytest.raises(KafkaTimeoutError):
            producer.send(topic, value=b"refused")
        accepted = producer.send(topic, value=b"accepted").get(timeout=10)

        assert [recorded.accessor for recorded in recording.built] == ["group_consumer", "producer"]
        assert recording.clients_of("group_consumer")[0].kwargs["client_id"] == "recorded-0"
        assert consumer.wrapped.subscription() == {topic}
        assert [call.method for call in consumer.calls] == ["subscribe"]
        assert [call.method for call in producer.calls] == ["send", "send"]
        assert accepted.offset == 0
