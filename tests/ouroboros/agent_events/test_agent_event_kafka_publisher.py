from __future__ import annotations

import time

import pytest
from kafka.errors import KafkaTimeoutError
from kafka.partitioner.default import murmur2

from ouroboros.agent_events.interfaces import OutboundText, TaskHeartbeat
from ouroboros.agent_events.kafka_transport import KafkaAgentEventPublisher
from tests.helpers.agent_event_conformance import POLL_TIMEOUT_SEC
from tests.helpers.kafka_agent_events import KafkaAgentEventRig
from tests.helpers.kafka_helpers import TEST_EVENT_TOPIC_PARTITIONS, fresh_prefix, kafka_connection
from tests.helpers.shared import TEST_SCOPE, scope_with

CONNECTION = kafka_connection(fresh_prefix())
UNDRAINED_REPORTS = 600
_TRANSPORT = "ouroboros.agent_events.kafka_transport"


def test_a_report_is_appended_with_its_scope_key_on_the_partition_the_library_maps_it_to() -> None:
    rig = KafkaAgentEventRig(connection=CONNECTION)
    publisher = rig.publisher()
    scopes = [scope_with(TEST_SCOPE, chat_id=f"chat-{index}") for index in range(12)]

    for scope in scopes:
        publisher.publish(OutboundText(scope=scope, text=scope.chat_id))

    partitions = sorted(range(TEST_EVENT_TOPIC_PARTITIONS))
    expected = [
        partitions[(murmur2(KafkaAgentEventPublisher.record_key(scope)) & 0x7FFFFFFF) % len(partitions)]
        for scope in scopes
    ]
    sends = rig.clients.clients_of("producer")[0].client.calls_of("send")
    assert [call.kwargs["key"] for call in sends] == [KafkaAgentEventPublisher.record_key(scope) for scope in scopes]
    assert ["partition" in call.kwargs for call in sends] == [False] * len(scopes)
    assert [rig.cluster.end_offset(rig.topic, partition) for partition in partitions] == [
        expected.count(partition) for partition in partitions
    ]


def test_reports_nobody_drains_are_appended_without_waiting_or_reading_an_offset() -> None:
    rig = KafkaAgentEventRig(connection=CONNECTION)
    publisher = rig.publisher()
    scope = rig.scope_on(0)

    started = time.monotonic()
    for index in range(UNDRAINED_REPORTS):
        event = OutboundText(scope=scope, text=str(index)) if index % 2 else TaskHeartbeat(scope=scope, task_id="t-1")
        publisher.publish(event)
    elapsed = time.monotonic() - started

    assert rig.cluster.end_offset(rig.topic, 0) == UNDRAINED_REPORTS
    assert elapsed < POLL_TIMEOUT_SEC * 10
    assert {recorded.accessor for recorded in rig.clients.built} == {"producer"}
    assert {call.method for call in rig.clients.clients_of("producer")[0].client.calls} == {"send"}


def test_a_publish_that_timed_out_during_an_outage_lands_once_after_it_heals(monkeypatch) -> None:
    rig = KafkaAgentEventRig(connection=CONNECTION)
    publisher, consumer = rig.publisher(), rig.consumer("supervisor-a")
    publisher.publish(OutboundText(scope=TEST_SCOPE, text="before the outage"))
    assert consumer.poll(POLL_TIMEOUT_SEC).text == "before the outage"
    monkeypatch.setattr(f"{_TRANSPORT}._SEND_TIMEOUT_SEC", 0.1)
    rig.cluster.set_outage(True)

    with pytest.raises(KafkaTimeoutError, match="Timeout after waiting"):
        publisher.publish(OutboundText(scope=TEST_SCOPE, text="published into the outage"))
    rig.cluster.set_outage(False)

    assert consumer.poll(POLL_TIMEOUT_SEC).text == "published into the outage"
    assert consumer.poll(POLL_TIMEOUT_SEC) is None
    assert len(rig.clients.clients_of("producer")[0].client.calls_of("send")) == 2
