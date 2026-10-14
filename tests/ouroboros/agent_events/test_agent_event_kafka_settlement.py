from __future__ import annotations

import json
import logging
import ssl
import time

from kafka.errors import CommitFailedError, KafkaTimeoutError

from ouroboros.agent_events.interfaces import OutboundText
from tests.helpers.agent_event_conformance import POLL_TIMEOUT_SEC
from tests.helpers.kafka_agent_events import KafkaAgentEventRig
from tests.helpers.kafka_helpers import fresh_prefix, kafka_connection
from tests.helpers.shared import TEST_SCOPE

CONNECTION = kafka_connection(fresh_prefix())
_LOGGER = "ouroboros.agent_events.kafka_transport"


def test_a_report_whose_commit_failed_is_still_returned_and_settled_before_the_next_fetch(monkeypatch, caplog) -> None:
    monkeypatch.setattr("ouroboros.agent_events.kafka_transport.BROKER_RETRY_SEC", 0.05)
    rig = KafkaAgentEventRig(connection=CONNECTION)
    publisher, consumer = rig.publisher(), rig.consumer("supervisor-a")
    assert consumer.poll() is None
    for text in ("report 1", "report 2"):
        publisher.publish(OutboundText(scope=TEST_SCOPE, text=text))
    group_consumer = rig.group_consumer("supervisor-a")
    group_consumer.fail_next("commit", KafkaTimeoutError("the coordinator did not answer"))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert consumer.poll(0.0).text == "report 1"
    assert rig.committed(TEST_SCOPE) is None
    assert [record for record in caplog.records if rig.topic in record.getMessage()]
    assert not [record for record in caplog.records if "can be read again" in record.getMessage()]

    time.sleep(0.06)
    calls_before = len(group_consumer.calls)
    assert consumer.poll(0.0).text == "report 2"
    calls = group_consumer.calls[calls_before:]

    steps = [(call.method, [taken.offset for taken in call.args[0].values()] if call.args else []) for call in calls]
    assert steps.index(("commit", [1])) < [method for method, _ in steps].index("poll")
    assert rig.committed(TEST_SCOPE) == 2


def test_a_report_whose_commit_raised_a_tls_failure_is_reported_like_any_other_unanswered_settlement(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr("ouroboros.agent_events.kafka_transport.BROKER_RETRY_SEC", 0.05)
    rig = KafkaAgentEventRig(connection=CONNECTION)
    publisher, consumer = rig.publisher(), rig.consumer("supervisor-a")
    assert consumer.poll() is None
    for text in ("report 1", "report 2"):
        publisher.publish(OutboundText(scope=TEST_SCOPE, text=text))
    group_consumer = rig.group_consumer("supervisor-a")
    group_consumer.fail_next("commit", ssl.SSLError("tlsv13 alert certificate required"))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert consumer.poll(0.0).text == "report 1"
    assert rig.committed(TEST_SCOPE) is None
    assert [record for record in caplog.records if "SSLError" in record.getMessage()]

    time.sleep(0.06)
    assert consumer.poll(0.0).text == "report 2"
    assert rig.committed(TEST_SCOPE) == 2


def test_a_skipped_record_whose_commit_timed_out_ends_the_poll_after_that_one_commit(monkeypatch) -> None:
    monkeypatch.setattr("ouroboros.agent_events.kafka_transport.BROKER_RETRY_SEC", 0.5)
    rig = KafkaAgentEventRig(connection=CONNECTION)
    consumer = rig.consumer("supervisor-a")
    assert consumer.poll() is None
    unknown = json.dumps({"kind": "SomethingTheReaderHasNeverHeardOf", "payload": "{}"}).encode()
    rig.cluster.append_raw(rig.topic, rig.partition_of(TEST_SCOPE), unknown)
    rig.publisher().publish(OutboundText(scope=TEST_SCOPE, text="the one after it"))
    group_consumer = rig.group_consumer("supervisor-a")
    group_consumer.fail_next("commit", KafkaTimeoutError("the coordinator did not answer"))
    calls_before = len(group_consumer.calls)

    assert consumer.poll(0.0) is None
    assert [call.method for call in group_consumer.calls[calls_before:]].count("commit") == 1
    assert consumer.poll(0.0) is None
    assert [call.method for call in group_consumer.calls[calls_before:]].count("commit") == 1

    time.sleep(0.55)
    assert consumer.poll(0.0).text == "the one after it"
    assert rig.committed(TEST_SCOPE) == 2


def test_a_settlement_that_keeps_timing_out_fetches_nothing_and_reports_the_outage(monkeypatch, caplog) -> None:
    monkeypatch.setattr("ouroboros.agent_events.kafka_transport.BROKER_RETRY_SEC", 0.0)
    rig = KafkaAgentEventRig(connection=CONNECTION)
    publisher, consumer = rig.publisher(), rig.consumer("supervisor-a")
    assert consumer.poll() is None
    for text in ("report 1", "report 2"):
        publisher.publish(OutboundText(scope=TEST_SCOPE, text=text))
    group_consumer = rig.group_consumer("supervisor-a")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        group_consumer.fail_next("commit", KafkaTimeoutError("the coordinator did not answer"))
        assert consumer.poll(0.0).text == "report 1"
        polls_before = len(group_consumer.calls_of("poll"))
        for _ in range(3):
            group_consumer.fail_next("commit", KafkaTimeoutError("the coordinator still does not answer"))
            assert consumer.poll(0.0) is None

    assert len(group_consumer.calls_of("poll")) == polls_before
    assert len([record for record in caplog.records if rig.topic in record.getMessage()]) == 1
    assert rig.committed(TEST_SCOPE) is None


def test_a_stale_settlement_does_not_wedge_the_consumer(monkeypatch, caplog) -> None:
    monkeypatch.setattr("ouroboros.agent_events.kafka_transport.BROKER_RETRY_SEC", 0.0)
    rig = KafkaAgentEventRig(connection=CONNECTION)
    publisher, consumer = rig.publisher(), rig.consumer("supervisor-a")
    assert consumer.poll() is None
    for text in ("report 1", "report 2"):
        publisher.publish(OutboundText(scope=TEST_SCOPE, text=text))
    group_consumer = rig.group_consumer("supervisor-a")
    group_consumer.fail_next("commit", KafkaTimeoutError("the coordinator did not answer"))
    assert consumer.poll(0.0).text == "report 1"
    group_consumer.fail_next("commit", CommitFailedError("the member is no longer part of the group"))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        polled = consumer.poll(0.0)

    assert polled is not None and polled.text == "report 2"
    assert any("delivered again" in record.getMessage() for record in caplog.records)
    assert rig.committed(TEST_SCOPE) == 2


def test_a_failed_commit_followed_by_a_partition_move_delivers_that_report_at_most_once_more() -> None:
    rig = KafkaAgentEventRig(connection=CONNECTION)
    publisher = rig.publisher()
    for index in range(4):
        publisher.publish(OutboundText(scope=TEST_SCOPE, text=str(index)))
    first, second = rig.consumer("supervisor-a"), rig.consumer("supervisor-b")
    delivered = [first.poll(POLL_TIMEOUT_SEC).text]
    rig.group_consumer("supervisor-a").fail_next("commit", KafkaTimeoutError("the coordinator did not answer"))
    delivered.append(first.poll(0.0).text)

    rig.cluster.crash(rig.group_consumer("supervisor-a").wrapped)
    taken_over = []
    while (event := second.poll(POLL_TIMEOUT_SEC)) is not None:
        taken_over.append(event.text)

    assert delivered == ["0", "1"]
    assert taken_over == ["1", "2", "3"]


def test_a_failed_commit_followed_by_the_same_consumer_rejoining_delivers_that_report_once_more(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr("ouroboros.agent_events.kafka_transport.BROKER_RETRY_SEC", 0.0)
    rig = KafkaAgentEventRig(connection=CONNECTION)
    publisher = rig.publisher()
    for index in range(4):
        publisher.publish(OutboundText(scope=TEST_SCOPE, text=str(index)))
    consumer = rig.consumer("supervisor-a")
    delivered = [consumer.poll(POLL_TIMEOUT_SEC).text]
    group_consumer = rig.group_consumer("supervisor-a")
    group_consumer.fail_next("commit", KafkaTimeoutError("the coordinator did not answer"))
    delivered.append(consumer.poll(0.0).text)

    rig.cluster.crash(group_consumer.wrapped)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        while (event := consumer.poll(POLL_TIMEOUT_SEC)) is not None:
            delivered.append(event.text)

    assert delivered == ["0", "1", "1", "2", "3"]
    assert any("delivered again" in record.getMessage() for record in caplog.records)
    assert rig.committed(TEST_SCOPE) == 4
