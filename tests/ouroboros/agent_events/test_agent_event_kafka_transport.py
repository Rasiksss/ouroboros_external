from __future__ import annotations

import json
import logging
import pathlib
import ssl
import time
import uuid

import pytest
from kafka.errors import KafkaError

from infra.kafka_client import KAFKA_ZERO_TIMEOUT_FETCH_MS, IKafkaClientFactory, KafkaOffsetReset
from ouroboros.agent_events.interfaces import OutboundText, TaskHeartbeat
from ouroboros.agent_events.kafka_transport import (
    KAFKA_AGENT_EVENT_MAX_POLL_INTERVAL_MS,
    MAX_SKIPPED_RECORDS_PER_POLL,
    KafkaAgentEventConsumer,
    KafkaAgentEventPublisher,
)
from tests.helpers.agent_event_conformance import POLL_TIMEOUT_SEC, AgentEventConformance
from tests.helpers.kafka_agent_events import KafkaAgentEventRig, kafka_agent_event_transport
from tests.helpers.kafka_double import KafkaDoubleClientFactory, KafkaDoubleCluster
from tests.helpers.kafka_helpers import (
    TEST_EVENT_TOPIC_PARTITIONS,
    await_assignment,
    create_broker_topics,
    create_fleet_topics,
    fleet_topics,
    fresh_prefix,
    kafka_connection,
    needs_kafka,
    real_client_factory,
    release_real_broker,
)
from tests.helpers.kafka_recording import KafkaRecordingClientFactory
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with

CONNECTION = kafka_connection(fresh_prefix())
_LOGGER = "ouroboros.agent_events.kafka_transport"
_RELEASED: list[tuple[KafkaRecordingClientFactory, str]] = []


@pytest.fixture(autouse=True)
def _release_real_brokers():
    yield
    for recording, prefix in _RELEASED:
        release_real_broker(recording, prefix)
    _RELEASED.clear()


class _KafkaAgentEventConformance(AgentEventConformance):
    def preserves_order_within_one_scope(self) -> bool:
        return True


class TestDoubleKafkaTransport(_KafkaAgentEventConformance):
    def make_transport(self) -> tuple[KafkaAgentEventPublisher, KafkaAgentEventConsumer]:
        cluster = KafkaDoubleCluster()
        create_fleet_topics(cluster, CONNECTION)
        clients = KafkaDoubleClientFactory(cluster=cluster)
        return kafka_agent_event_transport(clients, CONNECTION, client_id=f"supervisor-{uuid.uuid4().hex}")


class TestKafkaTransport(_KafkaAgentEventConformance):
    pytestmark = needs_kafka

    def make_transport(self) -> tuple[KafkaAgentEventPublisher, KafkaAgentEventConsumer]:
        prefix = fresh_prefix()
        factory = real_client_factory(prefix)
        recording = KafkaRecordingClientFactory(clients=factory)
        _RELEASED.append((recording, prefix))
        create_broker_topics(fleet_topics(factory.connection))
        publisher, consumer = kafka_agent_event_transport(
            recording, factory.connection, client_id=f"supervisor-{uuid.uuid4().hex}"
        )
        await_assignment(consumer, clients=recording)
        return publisher, consumer


class _RefusingClients(IKafkaClientFactory):
    def async_producer(self):
        raise AssertionError("no async producer may be built here")

    def producer(self, *, topic):
        raise AssertionError(f"no producer for {topic} may be built here")

    def group_consumer(self, *, group_id, client_id, auto_offset_reset: KafkaOffsetReset, max_poll_interval_ms):
        raise AssertionError(f"no group consumer of {group_id} may be built here")

    def async_group_consumer(self, *, group_id, client_id, auto_offset_reset: KafkaOffsetReset, max_poll_interval_ms):
        raise AssertionError(f"no group consumer of {group_id} may be built here")

    def partition_reader(self, *, auto_offset_reset: KafkaOffsetReset):
        raise AssertionError("no partition reader may be built here")


_ARGUMENTS = {
    KafkaAgentEventPublisher: {"topic": "fleet-a.agent-events"},
    KafkaAgentEventConsumer: {
        "topic": "fleet-a.agent-events",
        "group_id": "fleet-a.agent-events.supervisor",
        "client_id": "supervisor-a",
        "max_poll_interval_ms": KAFKA_AGENT_EVENT_MAX_POLL_INTERVAL_MS,
    },
}


def _refusing(half):
    return half(clients=_RefusingClients(), **_ARGUMENTS[half])


def test_constructors_build_no_client() -> None:
    _refusing(KafkaAgentEventPublisher)
    _refusing(KafkaAgentEventConsumer)


def test_the_publisher_refuses_anything_that_is_not_a_report() -> None:
    with pytest.raises(TypeError):
        _refusing(KafkaAgentEventPublisher).publish({"scope": TEST_SCOPE, "text": "hello"})


def test_a_value_no_wire_format_carries_is_refused_before_any_client() -> None:
    unsafe = OutboundText(scope=TEST_SCOPE, text="hi", transport={"hint": pathlib.Path("/tmp/x")})

    with pytest.raises(TypeError):
        _refusing(KafkaAgentEventPublisher).publish(unsafe)


def test_the_record_key_is_canonical_over_all_four_axes() -> None:
    scope = scope_with(TEST_SCOPE, owner_id="owner", agent_id="agent", surface_id="browser", chat_id="chat")
    variants = [
        scope_with(scope, owner_id="other"),
        scope_with(scope, agent_id="other"),
        scope_with(scope, surface_id="dialogs"),
        scope_with(scope, chat_id="other"),
    ]

    assert KafkaAgentEventPublisher.record_key(scope) == b'["owner","agent","browser","chat"]'
    assert len({KafkaAgentEventPublisher.record_key(item) for item in [scope, *variants]}) == 5
    assert KafkaAgentEventPublisher.record_key(scope_with(scope)) == KafkaAgentEventPublisher.record_key(scope)


def test_both_halves_answer_the_partition_key_with_the_scope() -> None:
    publisher, consumer = _refusing(KafkaAgentEventPublisher), _refusing(KafkaAgentEventConsumer)
    other = scope_with(TEST_SCOPE, owner_id="someone-else")

    for event in (OutboundText(scope=TEST_SCOPE), TaskHeartbeat(scope=other)):
        assert publisher.partition_key(event) is event.scope
        assert consumer.partition_key(event) is event.scope


def test_the_first_poll_subscribes_and_construction_does_not() -> None:
    rig = KafkaAgentEventRig(connection=CONNECTION)
    consumer = rig.consumer("supervisor-a")

    assert rig.clients.clients_of("group_consumer") == []
    consumer.poll()
    consumer.poll()

    assert [recorded.kwargs for recorded in rig.clients.clients_of("group_consumer")] == [
        {
            "group_id": rig.group_id,
            "client_id": "supervisor-a",
            "auto_offset_reset": KafkaOffsetReset.EARLIEST,
            "max_poll_interval_ms": KAFKA_AGENT_EVENT_MAX_POLL_INTERVAL_MS,
        }
    ]
    assert [call.args for call in rig.group_consumer("supervisor-a").calls_of("subscribe")] == [([rig.topic],)]


def test_a_report_published_before_any_supervisor_existed_is_delivered() -> None:
    rig = KafkaAgentEventRig(connection=CONNECTION)
    rig.publisher().publish(OutboundText(scope=TEST_SCOPE, text="published before the supervisor started"))

    assert rig.consumer("supervisor-a").poll(POLL_TIMEOUT_SEC).text == "published before the supervisor started"


def test_a_report_is_settled_before_it_is_returned() -> None:
    rig = KafkaAgentEventRig(connection=CONNECTION)
    publisher, consumer = rig.publisher(), rig.consumer("supervisor-a")
    for index in range(3):
        publisher.publish(OutboundText(scope=TEST_SCOPE, text=str(index)))

    for index in range(3):
        assert consumer.poll(POLL_TIMEOUT_SEC).text == str(index)
        assert rig.committed(TEST_SCOPE) == index + 1


def test_a_zero_timeout_poll_returns_a_report_already_published() -> None:
    rig = KafkaAgentEventRig(connection=CONNECTION)
    publisher, consumer = rig.publisher(), rig.consumer("supervisor-a")
    assert consumer.poll() is None
    publisher.publish(OutboundText(scope=TEST_SCOPE, text="the first report after an idle pass"))
    fetches_before = len(rig.group_consumer("supervisor-a").calls_of("poll"))

    started = time.monotonic()
    polled = consumer.poll(0.0)

    assert polled is not None and polled.text == "the first report after an idle pass"
    assert time.monotonic() - started < KAFKA_ZERO_TIMEOUT_FETCH_MS / 1000 + 0.25
    fetches = rig.group_consumer("supervisor-a").calls_of("poll")[fetches_before:]
    assert [call.kwargs for call in fetches] == [{"timeout_ms": KAFKA_ZERO_TIMEOUT_FETCH_MS, "max_records": 1}]


UNDECODABLE_VALUES = {
    "no value": {"value": None},
    "not UTF-8": {"value": b"\xff"},
    "not JSON": {"value": b"{"},
    "a JSON array": {"value": b"[]"},
    "JSON null": {"value": b"null"},
    "a payload that does not decode": {"value": json.dumps({"kind": "OutboundText", "payload": "[]"}).encode()},
}


@parametrize_from_dict(UNDECODABLE_VALUES)
def test_an_undecodable_record_does_not_stall_the_drain(value, caplog) -> None:
    rig = KafkaAgentEventRig(connection=CONNECTION)
    rig.cluster.append_raw(rig.topic, rig.partition_of(TEST_SCOPE), value)
    rig.publisher().publish(OutboundText(scope=TEST_SCOPE, text="the one after it"))

    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        polled = rig.consumer("supervisor-a").poll(POLL_TIMEOUT_SEC)

    assert polled is not None and polled.text == "the one after it"
    assert any("does not decode" in record.getMessage() for record in caplog.records)
    assert rig.committed(TEST_SCOPE) == 2


def test_an_unknown_kind_is_recorded_and_skipped(caplog) -> None:
    rig = KafkaAgentEventRig(connection=CONNECTION)
    unknown = json.dumps({"kind": "SomethingTheReaderHasNeverHeardOf", "payload": "{}"}).encode()
    rig.cluster.append_raw(rig.topic, rig.partition_of(TEST_SCOPE), unknown)
    rig.publisher().publish(OutboundText(scope=TEST_SCOPE, text="the one after it"))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        polled = rig.consumer("supervisor-a").poll(POLL_TIMEOUT_SEC)

    assert polled is not None and polled.text == "the one after it"
    assert any("SomethingTheReaderHasNeverHeardOf" in record.getMessage() for record in caplog.records)
    assert rig.committed(TEST_SCOPE) == 2


def test_a_backlog_of_unreadable_records_returns_to_the_tick() -> None:
    rig = KafkaAgentEventRig(connection=CONNECTION)
    for _ in range(MAX_SKIPPED_RECORDS_PER_POLL + 4):
        rig.cluster.append_raw(rig.topic, rig.partition_of(TEST_SCOPE), b"{")
    rig.publisher().publish(OutboundText(scope=TEST_SCOPE, text="the one after them"))
    consumer = rig.consumer("supervisor-a")

    assert consumer.poll(0.0) is None
    assert consumer.poll(0.0).text == "the one after them"


def test_an_idle_consumer_asks_the_broker_nothing_beyond_its_fetch(caplog) -> None:
    rig = KafkaAgentEventRig(connection=CONNECTION)
    consumer = rig.consumer("supervisor-a")
    assert consumer.poll() is None
    group_consumer = rig.group_consumer("supervisor-a")
    issued = len(group_consumer.calls)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        for outage in (False, True):
            rig.cluster.set_outage(outage)
            polled = [consumer.poll(0.0) for _ in range(5)]
            assert polled == [None] * 5
        rig.cluster.set_outage(False)

    assert {call.method for call in group_consumer.calls[issued:]} == {"poll"}
    assert {recorded.accessor for recorded in rig.clients.built} == {"group_consumer"}
    assert [record for record in caplog.records if rig.topic in record.getMessage()] == []


def test_a_broker_that_comes_back_is_drained_without_a_restart(monkeypatch, caplog) -> None:
    monkeypatch.setattr("ouroboros.agent_events.kafka_transport.BROKER_RETRY_SEC", 0.05)
    rig = KafkaAgentEventRig(connection=CONNECTION)
    rig.publisher().publish(OutboundText(scope=TEST_SCOPE, text="published while the broker was up"))
    consumer = rig.consumer("supervisor-a")
    rig.cluster.set_outage(True)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert consumer.poll(0.0) is None
        rig.cluster.set_outage(False)
        time.sleep(0.06)
        polled = consumer.poll(1.0)

    assert polled is not None and polled.text == "published while the broker was up"
    assert [record for record in caplog.records if rig.topic in record.getMessage()] == []


def test_a_poll_never_raises_an_assertion_a_kafka_error_or_a_tls_failure(monkeypatch) -> None:
    monkeypatch.setattr("ouroboros.agent_events.kafka_transport.BROKER_RETRY_SEC", 0.0)
    rig = KafkaAgentEventRig(connection=CONNECTION)
    consumer = rig.consumer("supervisor-a")
    assert consumer.poll() is None
    group_consumer = rig.group_consumer("supervisor-a")

    for error in (
        AssertionError("KafkaConsumer is closed"),
        KafkaError("the principal may not read the group"),
        ssl.SSLCertVerificationError("certificate verify failed"),
    ):
        polls_before = len(group_consumer.calls_of("poll"))
        group_consumer.fail_next("poll", error)
        assert consumer.poll(0.0) is None
        assert len(group_consumer.calls_of("poll")) == polls_before + 1


def test_one_scope_stays_in_order_across_two_consumers_and_a_rebalance() -> None:
    rig = KafkaAgentEventRig(connection=CONNECTION)
    publisher = rig.publisher()
    scopes = [rig.scope_on(0), rig.scope_on(TEST_EVENT_TOPIC_PARTITIONS - 1)]
    published = {scope.chat_id: [f"{scope.chat_id}-{index}" for index in range(6)] for scope in scopes}
    for index in range(6):
        for scope in scopes:
            publisher.publish(OutboundText(scope=scope, text=published[scope.chat_id][index]))
    consumers = {"a": rig.consumer("supervisor-a"), "b": rig.consumer("supervisor-b")}
    delivered: list[tuple[str, OutboundText]] = []

    for _ in range(4):
        for name, consumer in consumers.items():
            if (event := consumer.poll(0.0)) is not None:
                delivered.append((name, event))
    rig.cluster.crash(rig.group_consumer("supervisor-a").wrapped)
    while (event := consumers["b"].poll(POLL_TIMEOUT_SEC)) is not None:
        delivered.append(("b", event))

    assert any({name for name, event in delivered if event.scope == scope} == {"a", "b"} for scope in scopes)
    for scope in scopes:
        assert [event.text for _, event in delivered if event.scope == scope] == published[scope.chat_id]
