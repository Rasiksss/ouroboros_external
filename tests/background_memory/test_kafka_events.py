from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

import pytest
from kafka.errors import MessageSizeTooLargeError

import infra.kafka_client as kafka_client
from background_memory.infra.events import kafka_transport
from background_memory.infra.events.kafka_transport import (
    KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS,
    MAX_SKIPPED_RECORDS_PER_POLL,
    KafkaMemoryEventConsumer,
    KafkaMemoryEventPublisher,
)
from background_memory.interfaces import (
    ChatMessagePersistedEvent,
    IMemoryEventConsumer,
    IMemoryEventPublisher,
    MemoryEvent,
)
from infra.kafka_client import IKafkaClientFactory, KafkaConnection, KafkaOffsetReset
from tests.helpers.kafka_double import KafkaDoubleClientFactory, KafkaDoubleCluster
from tests.helpers.kafka_helpers import (
    create_broker_topics,
    create_fleet_topics,
    fleet_topics,
    fresh_prefix,
    kafka_connection,
    needs_kafka,
    real_client_factory,
    release_real_broker,
)
from tests.helpers.kafka_memory_events import (
    CONNECTION,
    MODULE,
    delivered_task_ids,
    fleet_double,
    memory_consumer,
    memory_group,
    memory_publisher,
    memory_topic,
    partition_of,
    start_consumers,
)
from tests.helpers.kafka_recording import KafkaRecordingClientFactory
from tests.helpers.memory_event_conformance import (
    DELIVERY_TIMEOUT_SEC,
    MemoryEventConformance,
    MemoryEventPair,
    distillation_event,
    poll,
    task_id_of,
)
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with

TOPIC = memory_topic(CONNECTION)
OUTAGE_BOUND_SEC = 0.2
SCHEDULING_SLACK_SEC = 0.5
SMALL_CEILING_BYTES = 256


class _KafkaMemoryCases(MemoryEventConformance):
    clients: KafkaRecordingClientFactory
    connection: KafkaConnection

    def fresh_clients(self) -> KafkaRecordingClientFactory:
        raise NotImplementedError("a subclass says which Kafka these run against")

    def create_topics(self) -> None:
        raise NotImplementedError("a subclass says how the fleet's topics come to exist")

    def make_publisher_and_consumer(self) -> MemoryEventPair:
        self.clients = self.fresh_clients()
        self.create_topics()
        publisher = memory_publisher(self.clients, self.connection)
        consumer = memory_consumer(self.clients, self.connection, client_id="background-memory-0")
        start_consumers(self.clients, [consumer])
        return publisher, consumer

    def second_consumer(self, consumer: IMemoryEventConsumer) -> IMemoryEventConsumer:
        assert isinstance(consumer, KafkaMemoryEventConsumer)
        second = memory_consumer(self.clients, self.connection, client_id="background-memory-1")
        start_consumers(self.clients, [consumer, second])
        return second

    def make_group_after_history(self, history: Sequence[MemoryEvent]) -> MemoryEventPair:
        self.clients = self.fresh_clients()
        self.create_topics()
        publisher = memory_publisher(self.clients, self.connection)
        for event in history:
            publisher.publish(event)
        consumer = memory_consumer(self.clients, self.connection, client_id="background-memory-0")
        start_consumers(self.clients, [consumer])
        return publisher, consumer

    def max_skipped_per_poll(self) -> int:
        return MAX_SKIPPED_RECORDS_PER_POLL

    def serializes_events(self) -> bool:
        return True


class TestKafkaDoubleMemoryEvents(_KafkaMemoryCases):
    cluster: KafkaDoubleCluster

    def fresh_clients(self) -> KafkaRecordingClientFactory:
        self.cluster = KafkaDoubleCluster()
        self.connection = CONNECTION
        return KafkaRecordingClientFactory(clients=KafkaDoubleClientFactory(cluster=self.cluster))

    def create_topics(self) -> None:
        create_fleet_topics(self.cluster, self.connection)

    def break_broker(self, publisher: IMemoryEventPublisher, consumer: IMemoryEventConsumer) -> bool:
        self.cluster.set_outage(True)
        return True

    def heal_broker(self, publisher: IMemoryEventPublisher, consumer: IMemoryEventConsumer) -> None:
        self.cluster.set_outage(False)

    def inject_raw_record(self, consumer: IMemoryEventConsumer, payload: bytes) -> bool:
        key = KafkaMemoryEventPublisher.record_key(TEST_SCOPE)
        self.cluster.append_raw(TOPIC, partition_of(TEST_SCOPE), payload, key=key)
        return True


class TestRealKafkaMemoryEvents(_KafkaMemoryCases):
    pytestmark = needs_kafka

    @pytest.fixture(autouse=True)
    def _prefix(self):
        prefix = fresh_prefix()
        factory = real_client_factory(prefix)
        self.connection = factory.connection
        self.clients = KafkaRecordingClientFactory(clients=factory)
        yield
        release_real_broker(self.clients, prefix)

    def fresh_clients(self) -> KafkaRecordingClientFactory:
        return self.clients

    def create_topics(self) -> None:
        create_broker_topics(fleet_topics(self.connection))

    def break_broker(self, publisher: IMemoryEventPublisher, consumer: IMemoryEventConsumer) -> bool:
        return False

    def inject_raw_record(self, consumer: IMemoryEventConsumer, payload: bytes) -> bool:
        topic = memory_topic(self.connection)
        key = KafkaMemoryEventPublisher.record_key(TEST_SCOPE)
        self.clients.wrapped.producer(topic=topic).send(topic, key=key, value=payload).get(timeout=DELIVERY_TIMEOUT_SEC)
        return True


class _RefusingClients(IKafkaClientFactory):
    def async_producer(self):
        raise AssertionError("no async producer may be built here")

    def producer(self, *, topic: str) -> Any:
        raise AssertionError("constructing a memory-event backend must build no producer")

    def group_consumer(
        self, *, group_id: str, client_id: str, auto_offset_reset: KafkaOffsetReset, max_poll_interval_ms: int
    ) -> Any:
        raise AssertionError("constructing a memory-event backend must build no group consumer")

    def async_group_consumer(
        self, *, group_id: str, client_id: str, auto_offset_reset: KafkaOffsetReset, max_poll_interval_ms: int
    ) -> Any:
        raise AssertionError("constructing a memory-event backend must build no group consumer")

    def partition_reader(self, *, auto_offset_reset: KafkaOffsetReset) -> Any:
        raise AssertionError("constructing a memory-event backend must build no partition reader")


class _ClosedProducer:
    def send(self, topic: str, value: bytes | None = None, key: bytes | None = None) -> Any:
        raise AssertionError("KafkaProducer already closed!")


class _ClosedProducerClients(KafkaDoubleClientFactory):
    def producer(self, *, topic: str) -> Any:
        return _ClosedProducer()


def _warnings_with_kind_and_scope(caplog: pytest.LogCaptureFixture, kind: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if kind in record.getMessage() and "scope=" in record.getMessage()]


def test_the_record_key_is_the_owner_and_agent_pair() -> None:
    key = KafkaMemoryEventPublisher.record_key

    assert key(TEST_SCOPE) == b'["global","main"]'
    assert key(scope_with(TEST_SCOPE, surface_id="dialogs", chat_id="2")) == key(TEST_SCOPE)
    assert key(scope_with(TEST_SCOPE, owner_id="someone-else")) != key(TEST_SCOPE)
    assert key(scope_with(TEST_SCOPE, agent_id="another-agent")) != key(TEST_SCOPE)


def test_two_events_of_one_pair_land_on_one_partition() -> None:
    cluster, clients = fleet_double()
    publisher = memory_publisher(clients, CONNECTION)
    partition = partition_of(TEST_SCOPE)
    before = cluster.end_offset(TOPIC, partition)

    publisher.publish(distillation_event("in-one-chat"))
    publisher.publish(ChatMessagePersistedEvent(scope=scope_with(TEST_SCOPE, chat_id="2"), message_id="in-another"))

    assert cluster.end_offset(TOPIC, partition) == before + 2


OUTAGE_WAITS = {
    "a producer that never learned the topic waits out the metadata bound": {"warmed": False},
    "a producer that knows the topic waits out the acknowledgement bound": {"warmed": True},
}


@parametrize_from_dict(OUTAGE_WAITS)
def test_every_publish_during_an_outage_waits_out_its_own_bound_and_warns(warmed, monkeypatch, caplog) -> None:
    monkeypatch.setattr(kafka_client, "_PRODUCER_MAX_BLOCK_MS", int(OUTAGE_BOUND_SEC * 1000))
    monkeypatch.setattr(kafka_transport, "_PUBLISH_WAIT_SEC", OUTAGE_BOUND_SEC)
    cluster, clients = fleet_double()
    publisher = memory_publisher(clients, CONNECTION)
    if warmed:
        publisher.publish(distillation_event("while-healthy"))
    cluster.set_outage(True)

    waits: list[float] = []
    with caplog.at_level(logging.WARNING, logger=MODULE):
        for task_id in ("first", "second", "third"):
            started = time.monotonic()
            assert publisher.publish(distillation_event(task_id)) is None
            waits.append(time.monotonic() - started)
    cluster.set_outage(False)

    assert all(OUTAGE_BOUND_SEC <= waited < 2 * OUTAGE_BOUND_SEC + SCHEDULING_SLACK_SEC for waited in waits), waits
    assert len(_warnings_with_kind_and_scope(caplog, "MemoryDistillationEvent")) == 3


def test_an_event_over_the_topics_limit_is_dropped_with_the_warning_through_the_librarys_refusal(caplog) -> None:
    cluster = KafkaDoubleCluster()
    connection = kafka_connection(CONNECTION.topic_prefix, max_message_bytes=SMALL_CEILING_BYTES)
    create_fleet_topics(cluster, connection)
    publisher = memory_publisher(KafkaDoubleClientFactory(cluster=cluster), connection)

    with caplog.at_level(logging.WARNING, logger=MODULE):
        assert publisher.publish(distillation_event("too-large")) is None

    warnings = _warnings_with_kind_and_scope(caplog, "MemoryDistillationEvent")
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None and isinstance(warnings[0].exc_info[1], MessageSizeTooLargeError)
    assert cluster.end_offset(TOPIC, partition_of(TEST_SCOPE)) == 0


def test_a_producer_closed_under_the_publisher_does_not_raise(caplog) -> None:
    publisher = KafkaMemoryEventPublisher(clients=_ClosedProducerClients(cluster=KafkaDoubleCluster()), topic=TOPIC)

    with caplog.at_level(logging.WARNING, logger=MODULE):
        assert publisher.publish(distillation_event("after-the-stop")) is None

    assert _warnings_with_kind_and_scope(caplog, "MemoryDistillationEvent")


def test_an_append_acknowledged_after_its_wait_is_delivered_once(monkeypatch, caplog) -> None:
    monkeypatch.setattr(kafka_transport, "_PUBLISH_WAIT_SEC", 0.1)
    cluster, clients = fleet_double()
    publisher = memory_publisher(clients, CONNECTION)
    consumer = memory_consumer(clients, CONNECTION, client_id="background-memory-0")
    start_consumers(clients, [consumer])
    publisher.publish(distillation_event("warm-up"))
    assert task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC)) == "warm-up"
    cluster.set_outage(True)

    with caplog.at_level(logging.WARNING, logger=MODULE):
        publisher.publish(distillation_event("landed-late"))
    cluster.set_outage(False)

    assert _warnings_with_kind_and_scope(caplog, "MemoryDistillationEvent")
    assert delivered_task_ids(consumer) == ["landed-late"]


def test_constructing_either_half_builds_no_client() -> None:
    KafkaMemoryEventPublisher(clients=_RefusingClients(), topic=TOPIC)
    KafkaMemoryEventConsumer(
        clients=_RefusingClients(),
        topic=TOPIC,
        group_id=memory_group(CONNECTION),
        client_id="background-memory-0",
        max_poll_interval_ms=KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS,
    )
