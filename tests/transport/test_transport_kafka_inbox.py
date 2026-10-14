from __future__ import annotations

import logging
import ssl
import time

import pytest
from kafka.errors import CommitFailedError, KafkaTimeoutError, RebalanceInProgressError, TopicAuthorizationFailedError

from infra.kafka_client import KAFKA_ZERO_TIMEOUT_FETCH_MS, KafkaOffsetReset
from tests.helpers.kafka_gateway import INBOUND_TOPIC, INBOX_CLIENT_ID, INBOX_GROUP_ID, KafkaGatewayDouble
from tests.helpers.transport_conformance import POLL_TIMEOUT_SEC, envelope
from transport.infra.kafka_gateway import KAFKA_INBOX_MAX_POLL_INTERVAL_MS, MAX_SKIPPED_RECORDS_PER_POLL, KafkaInbox

GATEWAY_LOGGER = "transport.infra.kafka_gateway"
SEND_BOUND_SEC = 0.2
FAST_RETRY_SEC = 0.05
SCHEDULING_SLACK_SEC = 0.15
EVICTING_POLL_INTERVAL_MS = 100
INBOUND_PARTITIONS = 4


def test_constructing_the_inbox_builds_no_client():
    double = KafkaGatewayDouble()

    double.inbox()

    assert double.clients.built == []


def test_submit_builds_the_topic_producer_and_never_a_consumer():
    double = KafkaGatewayDouble()
    inbox = double.inbox()

    inbox.submit(envelope("hello"))

    assert [(recorded.accessor, recorded.kwargs) for recorded in double.clients.built] == [
        ("producer", {"topic": INBOUND_TOPIC})
    ]
    assert double.cluster.end_offset(INBOUND_TOPIC, 0) == 1


def test_an_inbound_topic_with_several_partitions_is_polled_in_submission_order_from_partition_zero():
    double = KafkaGatewayDouble(inbound_partitions=INBOUND_PARTITIONS)
    inbox = double.inbox()
    submitted = [f"cmid-{index}" for index in range(8)]

    for client_message_id in submitted:
        inbox.submit(envelope(client_message_id, client_message_id=client_message_id))
    polled = [inbox.poll(POLL_TIMEOUT_SEC) for _ in submitted]

    assert [item.client_message_id if item is not None else None for item in polled] == submitted
    assert [double.cluster.end_offset(INBOUND_TOPIC, partition) for partition in range(INBOUND_PARTITIONS)] == [
        len(submitted),
        0,
        0,
        0,
    ]
    sends = [call for producer in double.built("producer") for call in producer.calls_of("send")]
    assert {call.kwargs["partition"] for call in sends} == {0}


def test_a_refused_append_makes_submit_raise():
    double = KafkaGatewayDouble()
    inbox = double.inbox()
    double.cluster.reject_next_send(INBOUND_TOPIC, TopicAuthorizationFailedError)

    with pytest.raises(TopicAuthorizationFailedError):
        inbox.submit(envelope("refused"))
    assert double.cluster.end_offset(INBOUND_TOPIC, 0) == 0


def test_submit_on_a_warm_producer_during_an_outage_raises_within_the_send_bound(monkeypatch):
    monkeypatch.setattr("transport.infra.kafka_gateway._SEND_TIMEOUT_SEC", SEND_BOUND_SEC)
    double = KafkaGatewayDouble()
    inbox = double.inbox()
    inbox.submit(envelope("warms the producer's metadata"))
    double.cluster.set_outage(True)

    started = time.monotonic()
    with pytest.raises(KafkaTimeoutError):
        inbox.submit(envelope("during the outage"))
    elapsed = time.monotonic() - started
    double.cluster.set_outage(False)

    assert elapsed < 0.5


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == level]


def _committed(double: KafkaGatewayDouble) -> int | None:
    return double.cluster.committed(INBOX_GROUP_ID, INBOUND_TOPIC, 0)


def test_the_first_poll_joins_one_group_consumer_under_the_inbox_identity_and_later_polls_build_nothing():
    double = KafkaGatewayDouble()
    inbox = double.inbox()

    inbox.poll(0.0)
    built = list(double.clients.built)
    inbox.poll(0.0)
    inbox.poll(0.0)

    consumers = double.clients.clients_of("group_consumer")
    assert [recorded.kwargs for recorded in consumers] == [
        {
            "group_id": INBOX_GROUP_ID,
            "client_id": INBOX_CLIENT_ID,
            "auto_offset_reset": KafkaOffsetReset.EARLIEST,
            "max_poll_interval_ms": KAFKA_INBOX_MAX_POLL_INTERVAL_MS,
        }
    ]
    assert [call.args for call in consumers[0].client.calls_of("subscribe")] == [([INBOUND_TOPIC],)]
    assert double.clients.built == built


def test_a_zero_timeout_poll_makes_one_bounded_fetch_of_one_record():
    double = KafkaGatewayDouble()
    inbox = double.inbox()

    assert inbox.poll(0.0) is None

    assert [call.kwargs for call in double.group_consumer().calls_of("poll")] == [
        {"timeout_ms": KAFKA_ZERO_TIMEOUT_FETCH_MS, "max_records": 1}
    ]


def test_a_settlement_that_times_out_is_kept_and_lands_before_the_next_envelope(monkeypatch, caplog):
    monkeypatch.setattr("transport.infra.kafka_gateway.BROKER_RETRY_SEC", FAST_RETRY_SEC)
    monkeypatch.setattr("transport.infra.kafka_gateway._SETTLE_TIMEOUT_MS", 100)
    double = KafkaGatewayDouble()
    inbox = double.inbox()
    inbox.submit(envelope("first", client_message_id="cmid-1"))
    inbox.submit(envelope("second", client_message_id="cmid-2"))
    assert inbox.poll(POLL_TIMEOUT_SEC).client_message_id == "cmid-1"
    consumer = double.group_consumer()
    fetches = len(consumer.calls_of("poll"))

    double.cluster.set_outage(True)
    with caplog.at_level(logging.WARNING, logger=GATEWAY_LOGGER):
        assert inbox.poll(0.0) is None
    assert len(consumer.calls_of("poll")) == fetches
    assert _committed(double) is None
    assert any(INBOUND_TOPIC in line for line in _messages(caplog, logging.WARNING))

    double.cluster.set_outage(False)
    time.sleep(FAST_RETRY_SEC * 2)
    second = inbox.poll(POLL_TIMEOUT_SEC)

    assert second is not None and second.client_message_id == "cmid-2"
    assert _committed(double) == 1
    assert [call.method for call in consumer.calls][-2:] == ["commit", "poll"]


@pytest.mark.parametrize(
    "error",
    [CommitFailedError(), RebalanceInProgressError("Offset commit cannot be completed during a rebalance")],
    ids=["the group rebalanced", "a rebalance is in progress"],
)
def test_a_stale_settlement_is_dropped_and_the_poll_proceeds_without_an_outage(error, caplog):
    double = KafkaGatewayDouble()
    inbox = double.inbox()
    for index in (1, 2, 3):
        inbox.submit(envelope(f"envelope {index}", client_message_id=f"cmid-{index}"))
    assert inbox.poll(POLL_TIMEOUT_SEC).client_message_id == "cmid-1"
    double.group_consumer().fail_next("commit", error)

    with caplog.at_level(logging.INFO, logger=GATEWAY_LOGGER):
        second = inbox.poll(POLL_TIMEOUT_SEC)
        third = inbox.poll(0.0)

    assert second is not None and second.client_message_id == "cmid-2"
    assert third is not None and third.client_message_id == "cmid-3"
    assert _committed(double) == 2
    assert _messages(caplog, logging.WARNING) == []
    assert any("stale" in line for line in _messages(caplog, logging.INFO))


def test_a_holder_whose_session_expired_gets_its_envelope_back_after_the_rejoin(caplog):
    double = KafkaGatewayDouble()
    inbox = double.inbox()
    inbox.submit(envelope("held across the expiry", client_message_id="cmid-held"))
    assert inbox.poll(POLL_TIMEOUT_SEC).client_message_id == "cmid-held"

    double.cluster.crash(double.group_consumer().wrapped)
    with caplog.at_level(logging.WARNING, logger=GATEWAY_LOGGER):
        again = inbox.poll(POLL_TIMEOUT_SEC)

    assert again is not None and again.client_message_id == "cmid-held"
    assert _messages(caplog, logging.WARNING) == []


def test_a_holder_that_stopped_polling_past_its_interval_loses_its_envelope_to_a_peer(caplog):
    double = KafkaGatewayDouble()
    holder = KafkaInbox(
        clients=double.clients,
        topic=INBOUND_TOPIC,
        group_id=INBOX_GROUP_ID,
        client_id="supervisor-stalled",
        max_poll_interval_ms=EVICTING_POLL_INTERVAL_MS,
    )
    peer = double.inbox()
    holder.submit(envelope("held past the interval", client_message_id="cmid-stalled"))
    assert holder.poll(POLL_TIMEOUT_SEC).client_message_id == "cmid-stalled"
    time.sleep(EVICTING_POLL_INTERVAL_MS / 1000 * 3)

    taken = peer.poll(POLL_TIMEOUT_SEC)
    stalled_consumer = double.built("group_consumer")[0]
    calls_before = len(stalled_consumer.calls)
    with caplog.at_level(logging.INFO, logger=GATEWAY_LOGGER):
        holder.poll(POLL_TIMEOUT_SEC)

    assert taken is not None and taken.client_message_id == "cmid-stalled"
    assert _messages(caplog, logging.WARNING) == []
    assert any("stale" in line for line in _messages(caplog, logging.INFO))
    later_calls = [call.method for call in stalled_consumer.calls[calls_before:]]
    assert later_calls[0] == "commit"
    assert later_calls.count("commit") == 1
    assert "poll" in later_calls[1:]
    assert _committed(double) is None


def test_an_idle_inbox_asks_the_broker_nothing_beyond_its_fetch_and_a_pending_settlement(caplog):
    double = KafkaGatewayDouble()
    inbox = double.inbox()
    inbox.submit(envelope("settled by the next poll", client_message_id="cmid-settled"))
    assert inbox.poll(POLL_TIMEOUT_SEC).client_message_id == "cmid-settled"
    consumer = double.group_consumer()
    issued = len(consumer.calls)
    built = len(double.clients.built)
    bound = KAFKA_ZERO_TIMEOUT_FETCH_MS / 1000 + SCHEDULING_SLACK_SEC

    with caplog.at_level(logging.WARNING, logger=GATEWAY_LOGGER):
        for outage in (False, True):
            double.cluster.set_outage(outage)
            for _ in range(5):
                polled_at = time.monotonic()
                assert inbox.poll(0.0) is None
                assert time.monotonic() - polled_at <= bound
        double.cluster.set_outage(False)

    assert [call.method for call in consumer.calls[issued:]] == ["commit"] + ["poll"] * 10
    assert len(double.clients.built) == built
    assert _messages(caplog, logging.WARNING) == []


def test_undecodable_records_are_skipped_on_a_bound_and_settled_cumulatively(caplog):
    double = KafkaGatewayDouble()
    inbox = double.inbox()
    for _ in range(40):
        double.cluster.append_raw(INBOUND_TOPIC, 0, b"{not json")
    inbox.submit(envelope("readable", client_message_id="cmid-readable"))

    with caplog.at_level(logging.ERROR, logger=GATEWAY_LOGGER):
        first = inbox.poll(POLL_TIMEOUT_SEC)
        skipped_first = len(_messages(caplog, logging.ERROR))
        committed_after_first = _committed(double)
        second = inbox.poll(POLL_TIMEOUT_SEC)
        committed_after_second = _committed(double)
        third = inbox.poll(0.0)

    assert first is None
    assert skipped_first == MAX_SKIPPED_RECORDS_PER_POLL
    assert len(_messages(caplog, logging.ERROR)) == 40
    assert committed_after_first is None
    assert second is not None and second.client_message_id == "cmid-readable"
    assert committed_after_second == MAX_SKIPPED_RECORDS_PER_POLL
    assert third is None
    assert _committed(double) == 41
    assert len(double.group_consumer().calls_of("commit")) == 2


@pytest.mark.parametrize(
    "error",
    [AssertionError("KafkaConsumer is closed"), ssl.SSLCertVerificationError("certificate verify failed")],
    ids=["a client assertion", "a tls failure outside the kafka error hierarchy"],
)
def test_a_client_failure_from_the_consumer_is_recorded_as_an_outage_rather_than_raised(error, monkeypatch, caplog):
    monkeypatch.setattr("transport.infra.kafka_gateway.BROKER_RETRY_SEC", FAST_RETRY_SEC)
    double = KafkaGatewayDouble()
    inbox = double.inbox()
    assert inbox.poll(0.0) is None
    double.group_consumer().fail_next("poll", error)

    with caplog.at_level(logging.WARNING, logger=GATEWAY_LOGGER):
        assert inbox.poll(0.0) is None
        reported = _messages(caplog, logging.WARNING)
        time.sleep(FAST_RETRY_SEC * 2)
        inbox.submit(envelope("after the failure", client_message_id="cmid-after"))
        delivered = inbox.poll(POLL_TIMEOUT_SEC)
        recovered = _messages(caplog, logging.WARNING)[len(reported) :]

    assert len(reported) == 1 and INBOUND_TOPIC in reported[0]
    assert delivered is not None and delivered.client_message_id == "cmid-after"
    assert len(recovered) == 1 and "again" in recovered[0]


def test_an_outage_on_an_idle_topic_clears_on_the_first_poll_that_answers(monkeypatch, caplog):
    monkeypatch.setattr("transport.infra.kafka_gateway.BROKER_RETRY_SEC", FAST_RETRY_SEC)
    double = KafkaGatewayDouble()
    inbox = double.inbox()
    assert inbox.poll(0.0) is None
    double.group_consumer().fail_next("poll", ssl.SSLCertVerificationError("certificate verify failed"))

    with caplog.at_level(logging.WARNING, logger=GATEWAY_LOGGER):
        assert inbox.poll(0.0) is None
        reported = _messages(caplog, logging.WARNING)
        time.sleep(FAST_RETRY_SEC * 2)
        assert inbox.poll(0.0) is None
        recovered = _messages(caplog, logging.WARNING)[len(reported) :]

    assert len(reported) == 1
    assert len(recovered) == 1 and "again" in recovered[0]


def test_a_second_outage_after_an_idle_recovery_carries_its_own_traceback(monkeypatch, caplog):
    monkeypatch.setattr("transport.infra.kafka_gateway.BROKER_RETRY_SEC", FAST_RETRY_SEC)
    double = KafkaGatewayDouble()
    inbox = double.inbox()
    assert inbox.poll(0.0) is None

    with caplog.at_level(logging.WARNING, logger=GATEWAY_LOGGER):
        for failure in (AssertionError("KafkaConsumer is closed"), OSError(113, "No route to host")):
            double.group_consumer().fail_next("poll", failure)
            assert inbox.poll(0.0) is None
            time.sleep(FAST_RETRY_SEC * 2)
            assert inbox.poll(0.0) is None

    outages = [record for record in caplog.records if record.exc_info is not None]
    assert [type(record.exc_info[1]).__name__ for record in outages] == ["AssertionError", "OSError"]


def test_the_submit_wait_covers_the_producers_own_give_up_so_a_timeout_means_not_accepted():
    from infra.kafka_client import _PRODUCER_DELIVERY_TIMEOUT_MS
    from transport.infra.kafka_gateway import _SEND_TIMEOUT_SEC

    assert _SEND_TIMEOUT_SEC * 1000 >= _PRODUCER_DELIVERY_TIMEOUT_MS
