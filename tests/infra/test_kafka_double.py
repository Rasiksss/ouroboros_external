from __future__ import annotations

import dataclasses
import inspect
import time

import pytest
from kafka.errors import TopicAuthorizationFailedError
from kafka.structs import OffsetAndMetadata, TopicPartition

import infra.kafka_client as kafka_client
from infra.kafka_client import IKafkaClientFactory, KafkaClientFactory, KafkaConnection, KafkaOffsetReset
from tests.helpers import kafka_helpers
from tests.helpers.kafka_client_behaviour import ANSWER_TIMEOUT_MS
from tests.helpers.kafka_double import KafkaDoubleClientFactory, KafkaDoubleCluster
from tests.helpers.kafka_group_behaviour import LAPSING_POLL_INTERVAL_MS, KafkaGroupBehaviour
from tests.helpers.kafka_helpers import (
    await_assignment,
    broker_admin,
    create_broker_topics,
    drop_prefixed_topics,
    fresh_prefix,
    needs_kafka,
    real_client_factory,
)
from tests.helpers.kafka_recording import KafkaRecordingClientFactory

CLOSE_TIMEOUT_SEC = 5.0
HEARTBEAT_LEAVE_MARGIN_SEC = 1.0


class TestKafkaClientBehaviourOnTheDouble(KafkaGroupBehaviour):
    @pytest.fixture(autouse=True)
    def _cluster(self, monkeypatch):
        monkeypatch.setattr("infra.kafka_client._GROUP_HEARTBEAT_INTERVAL_MS", 50)
        self.cluster = KafkaDoubleCluster()
        yield
        self.cluster.set_outage(False)

    def clients(self) -> IKafkaClientFactory:
        return KafkaDoubleClientFactory(cluster=self.cluster)

    def create_topic(self, name: str, *, partitions: int) -> None:
        self.cluster.create_topic(name, partitions=partitions, configs={})

    def unreachable_clients(self) -> IKafkaClientFactory:
        self.cluster.set_outage(True)
        return self.clients()

    def supports_healing(self) -> bool:
        return True

    def set_outage(self, active: bool) -> None:
        self.cluster.set_outage(active)

    def refuses_topic_auto_creation(self) -> bool:
        return True

    def crash(self, consumer) -> None:
        self.cluster.crash(consumer)

    def drop_membership(self, consumer) -> None:
        self.cluster.crash(consumer)

    def supports_retention_bound(self) -> bool:
        return True

    def retain_last(self, topic: str, count: int) -> None:
        self.cluster.retain_last(topic, count)

    def delete_up_to(self, topic: str, partition: int, offset: int) -> None:
        self.cluster.delete_up_to(topic, partition, offset)

    def size_limited_clients(self, topic: str, *, max_message_bytes: int) -> IKafkaClientFactory:
        self.cluster.create_topic(topic, partitions=1, configs={"max.message.bytes": str(max_message_bytes)})
        return self.clients()

    def topic_prefix(self) -> str:
        return "fleet-double"

    def test_a_call_outside_the_emulated_surface_does_not_exist(self):
        clients = self.clients()
        consumer = clients.group_consumer(
            group_id="fleet-double.group",
            client_id="surface-0",
            auto_offset_reset=KafkaOffsetReset.EARLIEST,
            max_poll_interval_ms=300_000,
        )
        reader = clients.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST)

        for client, name in (
            (consumer, "end_offsets"),
            (consumer, "committed"),
            (consumer, "seek"),
            (reader, "commit"),
            (reader, "subscribe"),
            (reader, "beginning_offsets"),
            (reader, "end_offsets"),
            (clients.producer(topic="fleet-double.any"), "metrics"),
            (clients, "admin"),
            (clients, "offset_reader"),
            (clients, "close"),
        ):
            assert not hasattr(client, name), name

    def test_the_double_refuses_every_poll_interval_the_production_factory_refuses(self):
        clients = self.clients()

        for refused in (0, -1, kafka_client.KAFKA_MAX_POLL_INTERVAL_CEILING_MS + 1, 720_000):
            for build in (clients.group_consumer, clients.async_group_consumer):
                with pytest.raises(RuntimeError, match="max_poll_interval_ms"):
                    build(
                        group_id="fleet-double.bounded",
                        client_id="bounded-0",
                        auto_offset_reset=KafkaOffsetReset.EARLIEST,
                        max_poll_interval_ms=refused,
                    )

    def test_a_rejected_send_fails_its_future_with_the_given_error_and_lands_nothing(self):
        self.create_topic("fleet-double.rejected", partitions=1)
        producer = self.clients().producer(topic="fleet-double.rejected")
        self.cluster.reject_next_send("fleet-double.rejected", TopicAuthorizationFailedError)

        with pytest.raises(TopicAuthorizationFailedError):
            producer.send("fleet-double.rejected", value=b"refused").get(timeout=1)
        assert producer.send("fleet-double.rejected", value=b"accepted").get(timeout=1).offset == 0
        assert self.cluster.end_offset("fleet-double.rejected", 0) == 1

    def test_raw_appends_and_commits_are_read_back_through_the_control_api(self):
        self.create_topic("fleet-double.raw", partitions=2)
        tp = TopicPartition("fleet-double.raw", 1)
        clients = self.clients()

        offsets = [self.cluster.append_raw("fleet-double.raw", 1, f"v{index}".encode(), key=b"k") for index in range(2)]
        member = clients.group_consumer(
            group_id="fleet-double.raw.g",
            client_id="raw-0",
            auto_offset_reset=KafkaOffsetReset.LATEST,
            max_poll_interval_ms=300_000,
        )
        member.subscribe(["fleet-double.raw"])
        assert member.poll(timeout_ms=0) == {}
        member.commit({tp: OffsetAndMetadata(2, "", -1)}, timeout_ms=1_000)
        reader = clients.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST)
        reader.assign([tp])

        assert offsets == [0, 1]
        assert [(record.key, record.value) for record in reader.poll(timeout_ms=1_000, max_records=2)[tp]] == [
            (b"k", b"v0"),
            (b"k", b"v1"),
        ]
        assert self.cluster.end_offset("fleet-double.raw", 1) == 2
        assert self.cluster.end_offset("fleet-double.raw", 0) == 0
        assert self.cluster.committed("fleet-double.raw.g", "fleet-double.raw", 1) == 2
        assert self.cluster.committed("fleet-double.raw.g", "fleet-double.raw", 0) is None

    def test_await_assignment_returns_once_the_consumer_holds_its_assignment(self):
        self.create_topic("fleet-double.harness", partitions=1)
        recording = KafkaRecordingClientFactory(clients=self.clients())
        reader = _SubscribedReader(clients=recording, topic="fleet-double.harness", group_id="fleet-double.harness.g")

        await_assignment(reader, clients=recording)

        assert recording.clients_of("group_consumer")[-1].client.wrapped.assignment() == {
            TopicPartition("fleet-double.harness", 0)
        }

    def test_await_assignment_fails_naming_the_group_when_no_assignment_comes(self, monkeypatch):
        monkeypatch.setattr(kafka_helpers, "ASSIGNMENT_TIMEOUT_SEC", 0.3)
        self.create_topic("fleet-double.harness", partitions=1)
        recording = KafkaRecordingClientFactory(clients=self.clients())
        reader = _SubscribedReader(clients=recording, topic="fleet-double.harness", group_id="fleet-double.harness.g")
        self.cluster.set_outage(True)

        started = time.monotonic()
        with pytest.raises(AssertionError, match="fleet-double.harness.g"):
            await_assignment(reader, clients=recording)
        assert time.monotonic() - started < 1.0

    def test_await_assignment_refuses_a_reader_that_hands_out_a_record(self):
        recording = KafkaRecordingClientFactory(clients=self.clients())

        with pytest.raises(AssertionError, match="took an item"):
            await_assignment(_HandingReader(), clients=recording)


class TestKafkaClientBehaviourOnABroker(KafkaGroupBehaviour):
    pytestmark = needs_kafka

    @pytest.fixture(autouse=True)
    def _prefix(self):
        self.prefix = fresh_prefix()
        self.factories: list[KafkaClientFactory] = []
        yield
        for factory in self.factories:
            for producer in factory._producers.values():
                producer.close(timeout=CLOSE_TIMEOUT_SEC)
        drop_prefixed_topics(self.prefix)

    def clients(self) -> IKafkaClientFactory:
        factory = real_client_factory(self.prefix)
        self.factories.append(factory)
        return factory

    def create_topic(self, name: str, *, partitions: int) -> None:
        create_broker_topics({name: partitions})

    def unreachable_clients(self) -> IKafkaClientFactory:
        factory = KafkaClientFactory()
        factory.initialize(
            dataclasses.replace(real_client_factory(self.prefix).connection, bootstrap_servers=("127.0.0.1:1",))
        )
        self.factories.append(factory)
        return factory

    def supports_healing(self) -> bool:
        return False

    def refuses_topic_auto_creation(self) -> bool:
        return False

    def crash(self, consumer) -> None:
        consumer.close(autocommit=False, timeout_ms=ANSWER_TIMEOUT_MS)

    def drop_membership(self, consumer) -> None:
        time.sleep(
            LAPSING_POLL_INTERVAL_MS / 1000
            + kafka_client._GROUP_HEARTBEAT_INTERVAL_MS / 1000
            + HEARTBEAT_LEAVE_MARGIN_SEC
        )

    def supports_retention_bound(self) -> bool:
        return False

    def delete_up_to(self, topic: str, partition: int, offset: int) -> None:
        admin = broker_admin()
        try:
            admin.delete_records({TopicPartition(topic, partition): offset}, timeout_ms=ANSWER_TIMEOUT_MS)
        finally:
            admin.close()

    def size_limited_clients(self, topic: str, *, max_message_bytes: int) -> IKafkaClientFactory:
        self.create_topic(topic, partitions=1)
        connection: KafkaConnection = real_client_factory(self.prefix).connection
        factory = KafkaClientFactory()
        factory.initialize(dataclasses.replace(connection, max_message_bytes=max_message_bytes))
        self.factories.append(factory)
        return factory

    def topic_prefix(self) -> str:
        return self.prefix


class _BrokerReached(Exception):
    pass


class _BrokerSuiteWithNoBroker(TestKafkaClientBehaviourOnABroker):
    def clients(self) -> IKafkaClientFactory:
        raise _BrokerReached

    def unreachable_clients(self) -> IKafkaClientFactory:
        raise _BrokerReached

    def topic_prefix(self) -> str:
        raise _BrokerReached


def test_the_broker_subclass_declines_by_name_only_what_a_broker_cannot_be_driven_into(monkeypatch):
    suite, declined = _BrokerSuiteWithNoBroker(), {}
    for name in sorted(name for name in dir(KafkaGroupBehaviour) if name.startswith("test_")):
        case = getattr(suite, name)
        arguments = {"monkeypatch": monkeypatch} if "monkeypatch" in inspect.signature(case).parameters else {}
        try:
            case(**arguments)
        except _BrokerReached:
            continue
        except pytest.skip.Exception as decline:
            declined[name] = decline.msg
        else:
            declined[name] = "returned without reaching the broker"

    assert declined == {
        "test_a_send_to_a_missing_topic_waits_out_the_metadata_bound_and_creates_nothing": (
            "_BrokerSuiteWithNoBroker declares refuses_topic_auto_creation() False"
        ),
        "test_a_standing_retention_bound_moves_the_log_start_under_an_earliest_reader_on_every_append": (
            "_BrokerSuiteWithNoBroker declares supports_retention_bound() False"
        ),
        "test_a_timed_out_send_lands_once_the_outage_heals": (
            "_BrokerSuiteWithNoBroker declares supports_healing() False"
        ),
    }


class _SubscribedReader:
    def __init__(self, *, clients: IKafkaClientFactory, topic: str, group_id: str) -> None:
        self._clients = clients
        self._topic = topic
        self._group_id = group_id
        self._consumer = None

    def poll(self, timeout: float):
        if self._consumer is None:
            self._consumer = self._clients.group_consumer(
                group_id=self._group_id,
                client_id="harness-0",
                auto_offset_reset=KafkaOffsetReset.EARLIEST,
                max_poll_interval_ms=300_000,
            )
            self._consumer.subscribe([self._topic])
        self._consumer.poll(timeout_ms=int(timeout * 1000))
        return None


class _HandingReader:
    def poll(self, timeout: float):
        return "a record"
