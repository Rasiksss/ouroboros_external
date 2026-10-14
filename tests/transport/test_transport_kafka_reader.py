from __future__ import annotations

import itertools
import logging
import ssl
import threading
import time

import pytest
from kafka.errors import KafkaTimeoutError, NotLeaderForPartitionError
from kafka.structs import TopicPartition

from infra.kafka_client import KafkaOffsetReset
from tests.helpers.kafka_gateway import OUTBOUND_TOPIC, KafkaGatewayDouble
from tests.helpers.kafka_recording import RecordingClient
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.transport_conformance import POLL_TIMEOUT_SEC, message
from transport.infra.kafka_gateway import _CLOSE_TIMEOUT_MS, MAX_SKIPPED_RECORDS_PER_POLL
from transport.interfaces import SubscriptionMode
from transport.reader_loop import run_reader_loop

GATEWAY_LOGGER = "transport.infra.kafka_gateway"
FAST_RETRY_SEC = 0.05
OUTBOUND_PARTITION = TopicPartition(OUTBOUND_TOPIC, 0)


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == level]


def _broadcast(double: KafkaGatewayDouble):
    return double.outbox().reader(name="browser", mode=SubscriptionMode.BROADCAST)


def test_a_reader_without_a_name_is_refused():
    double = KafkaGatewayDouble()

    with pytest.raises(ValueError, match="name"):
        double.outbox().reader(name="  ", mode=SubscriptionMode.BROADCAST)
    assert double.clients.built == []


def test_a_shared_reader_is_refused_naming_the_mode_and_kafka_before_any_client_is_built():
    double = KafkaGatewayDouble()

    with pytest.raises(ValueError) as refused:
        double.outbox().reader(name="dialogs", mode=SubscriptionMode.SHARED)

    assert "shared" in str(refused.value)
    assert "Kafka" in str(refused.value)
    assert double.clients.built == []


def test_a_broadcast_reader_is_one_partition_reader_that_resets_to_the_log_start_assigned_to_partition_zero():
    double = KafkaGatewayDouble()

    reader = _broadcast(double)
    try:
        assert [(recorded.accessor, recorded.kwargs) for recorded in double.clients.built] == [
            ("partition_reader", {"auto_offset_reset": KafkaOffsetReset.EARLIEST})
        ]
        assert [call.args for call in double.partition_reader().calls_of("assign")] == [([OUTBOUND_PARTITION],)]
    finally:
        reader.close()


def test_a_message_published_between_reader_and_its_first_poll_is_delivered():
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    outbox.publish(message("before the reader"))
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        outbox.publish(message("after reader returned"))

        delivered = reader.poll(0.0)

        assert delivered is not None
        assert delivered.message.text == "after reader returned"
    finally:
        reader.close()


def test_a_reader_opened_during_an_outage_raises_naming_itself_and_closes_its_consumer(monkeypatch):
    monkeypatch.setattr("transport.infra.kafka_gateway._READER_START_TIMEOUT_MS", 50)
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    double.cluster.set_outage(True)

    with pytest.raises(KafkaTimeoutError) as refused:
        outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    double.cluster.set_outage(False)

    assert "browser" in str(refused.value)
    assert OUTBOUND_TOPIC in str(refused.value)
    assert [call.kwargs for call in double.partition_reader().calls_of("close")] == [
        {"autocommit": False, "timeout_ms": _CLOSE_TIMEOUT_MS}
    ]


def _refuse_the_first_position(client: RecordingClient) -> None:
    client.fail_next("position", NotLeaderForPartitionError())


def _spend_the_first_position_without_answering(client: RecordingClient) -> None:
    answered = client.wrapped.position
    attempts = itertools.count()

    def _position(partition, timeout_ms=None):
        if next(attempts):
            return answered(partition, timeout_ms=timeout_ms)
        time.sleep((timeout_ms or 0) / 1_000)
        return None

    client.wrapped.position = _position


FIRST_ATTEMPT_REFUSALS = {
    "the attempt raises a refusal the broker answered with": {"arm": _refuse_the_first_position},
    "the attempt spends itself without a position": {"arm": _spend_the_first_position_without_answering},
}


@parametrize_from_dict(FIRST_ATTEMPT_REFUSALS)
def test_an_anchor_attempt_that_fails_is_asked_again_within_the_budget_and_the_reader_starts_at_the_end(
    arm, monkeypatch
):
    monkeypatch.setattr("transport.infra.kafka_gateway._READER_START_TIMEOUT_MS", 30_000)
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    build = double.clients.partition_reader

    def _armed(*, auto_offset_reset):
        client = build(auto_offset_reset=auto_offset_reset)
        arm(client)
        return client

    monkeypatch.setattr(double.clients, "partition_reader", _armed)
    outbox.publish(message("published before the reader opened"))

    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        outbox.publish(message("published after the reader opened"))
        delivered = reader.poll(POLL_TIMEOUT_SEC)

        assert delivered is not None and delivered.message.text == "published after the reader opened"
        assert len(double.partition_reader().calls_of("position")) == 2
        assert len(double.partition_reader().calls_of("assign")) == 1
        assert double.partition_reader().calls_of("close") == []
    finally:
        reader.close()


def test_a_held_delivery_stalls_the_reader_without_a_consumer_call_and_ack_frees_it_without_one():
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    consumer = double.partition_reader()
    try:
        outbox.publish(message("first"))
        outbox.publish(message("second"))
        first = reader.poll(POLL_TIMEOUT_SEC)
        assert first is not None and first.message.text == "first"
        calls = len(consumer.calls)

        assert reader.poll(0.0) is None
        assert reader.poll(0.1) is None
        reader.ack(first)
        assert len(consumer.calls) == calls

        second = reader.poll(POLL_TIMEOUT_SEC)
        assert second is not None and second.message.text == "second"
    finally:
        reader.close()


def test_a_nack_redelivers_under_a_new_receipt() -> None:
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        outbox.publish(message("delivery failed"))
        delivered = reader.poll(POLL_TIMEOUT_SEC)
        assert delivered is not None

        reader.nack(delivered)
        again = reader.poll(POLL_TIMEOUT_SEC)

        assert again is not None
        assert again.message == delivered.message
        assert again.receipt != delivered.receipt
        assert (delivered.attempts, again.attempts) == (0, 1)
    finally:
        reader.close()


def test_an_old_receipt_settles_nothing_and_duplicates_nothing():
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        outbox.publish(message("only"))
        old = reader.poll(POLL_TIMEOUT_SEC)
        reader.nack(old)
        current = reader.poll(POLL_TIMEOUT_SEC)
        assert current is not None
        seeks = len(double.partition_reader().calls_of("seek"))

        reader.ack(old)
        assert reader.poll(0.0) is None
        reader.nack(old)
        assert reader.poll(0.0) is None
        assert len(double.partition_reader().calls_of("seek")) == seeks

        reader.ack(current)
        assert reader.poll(POLL_TIMEOUT_SEC) is None
    finally:
        reader.close()


def test_a_receipt_settled_after_close_settles_nothing():
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    outbox.publish(message("in flight at close"))
    delivered = reader.poll(POLL_TIMEOUT_SEC)
    assert delivered is not None
    calls = len(double.partition_reader().calls)

    reader.close()
    reader.ack(delivered)
    reader.nack(delivered)

    assert reader.poll(0.0) is None
    assert [call.method for call in double.partition_reader().calls[calls:]] == ["close"]


def test_undecodable_records_are_skipped_on_a_bound(caplog):
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        for _ in range(40):
            double.cluster.append_raw(OUTBOUND_TOPIC, 0, b"{not json")
        outbox.publish(message("readable"))

        with caplog.at_level(logging.ERROR, logger=GATEWAY_LOGGER):
            first = reader.poll(POLL_TIMEOUT_SEC)
            skipped_first = len(_messages(caplog, logging.ERROR))
            second = reader.poll(POLL_TIMEOUT_SEC)

        assert first is None
        assert skipped_first == MAX_SKIPPED_RECORDS_PER_POLL
        assert second is not None and second.message.text == "readable"
    finally:
        reader.close()


def test_a_reader_whose_position_fell_below_the_log_start_resumes_there_without_raising_or_reporting(caplog):
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        for index in range(5):
            outbox.publish(message(f"m{index}"))
        double.cluster.delete_up_to(OUTBOUND_TOPIC, 0, 3)

        with caplog.at_level(logging.WARNING, logger=GATEWAY_LOGGER):
            resumed = reader.poll(POLL_TIMEOUT_SEC)

        assert resumed is not None and resumed.message.text == "m3"
        assert caplog.records == []
    finally:
        reader.close()


def test_a_reader_positioned_past_the_end_of_a_recreated_log_resumes_at_its_start_without_raising(caplog):
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        outbox.publish(message("first of the replaced log"))
        double.partition_reader().seek(OUTBOUND_PARTITION, 1000)

        with caplog.at_level(logging.WARNING, logger=GATEWAY_LOGGER):
            resumed = reader.poll(POLL_TIMEOUT_SEC)

        assert resumed is not None and resumed.message.text == "first of the replaced log"
        assert caplog.records == []
    finally:
        reader.close()


def test_an_idle_reader_asks_the_broker_nothing_beyond_its_fetch(caplog):
    double = KafkaGatewayDouble()
    reader = _broadcast(double)
    consumer = double.partition_reader()
    opened = len(consumer.calls)
    try:
        with caplog.at_level(logging.WARNING, logger=GATEWAY_LOGGER):
            for outage in (False, True):
                double.cluster.set_outage(outage)
                for timeout in (0.0, POLL_TIMEOUT_SEC):
                    assert reader.poll(timeout) is None
            double.cluster.set_outage(False)
    finally:
        reader.close()

    assert {call.method for call in consumer.calls[opened:]} == {"poll", "close"}
    assert caplog.records == []


def test_a_tls_failure_is_recorded_as_an_outage_and_the_reader_loop_keeps_delivering(monkeypatch, caplog):
    monkeypatch.setattr("transport.infra.kafka_gateway.BROKER_RETRY_SEC", FAST_RETRY_SEC)
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    reader = _broadcast(double)
    double.partition_reader().fail_next("poll", ssl.SSLCertVerificationError("certificate verify failed"))
    stop = threading.Event()
    delivered: list[str] = []

    def _handle(source, item) -> None:
        delivered.append(item.message.text)
        source.ack(item)
        stop.set()

    loop = threading.Thread(
        target=run_reader_loop,
        args=(reader, stop, _handle),
        kwargs={"name": "Browser", "poll_timeout_sec": POLL_TIMEOUT_SEC},
        name="kafka-reader-tls",
        daemon=True,
    )
    try:
        with caplog.at_level(logging.WARNING, logger=GATEWAY_LOGGER):
            loop.start()
            outbox.publish(message("after the handshake failed"))
            loop.join(POLL_TIMEOUT_SEC * 8)
        reported = _messages(caplog, logging.WARNING)
    finally:
        stop.set()
        loop.join(POLL_TIMEOUT_SEC * 4)
        reader.close()

    assert delivered == ["after the handshake failed"]
    assert "SSLCertVerificationError" in reported[0] or "certificate verify failed" in reported[0]
    assert "browser" in reported[0]


def test_close_with_no_poll_in_flight_releases_the_consumer_before_returning():
    double = KafkaGatewayDouble()
    reader = _broadcast(double)
    consumer = double.partition_reader()

    reader.close()

    assert [call.kwargs for call in consumer.calls_of("close")] == [
        {"autocommit": False, "timeout_ms": _CLOSE_TIMEOUT_MS}
    ]
    with pytest.raises(AssertionError):
        consumer.wrapped.poll(timeout_ms=0)
    started = time.monotonic()
    assert reader.poll(POLL_TIMEOUT_SEC) is None
    assert time.monotonic() - started < POLL_TIMEOUT_SEC / 2


def test_a_poll_racing_close_raises_nothing_and_the_polling_thread_releases_the_consumer():
    double = KafkaGatewayDouble()
    reader = _broadcast(double)
    consumer = double.partition_reader()
    errors: list[BaseException] = []
    closed = threading.Event()

    def _poll_until_closed() -> None:
        try:
            while not closed.is_set():
                reader.poll(POLL_TIMEOUT_SEC * 20)
            for _ in range(3):
                assert reader.poll(0.0) is None
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=_poll_until_closed, name="kafka-reader-race", daemon=True)
    thread.start()
    deadline = time.monotonic() + POLL_TIMEOUT_SEC * 4
    while not consumer.calls_of("poll") and time.monotonic() < deadline:
        time.sleep(0.001)

    reader.close()
    closed_by_close = list(consumer.calls_of("close"))
    closed.set()
    thread.join(POLL_TIMEOUT_SEC * 4)

    assert not thread.is_alive()
    assert errors == []
    assert closed_by_close == []
    assert len(consumer.calls_of("close")) == 1


def test_close_releases_a_poll_blocked_on_a_long_timeout():
    double = KafkaGatewayDouble()
    reader = _broadcast(double)
    returned = threading.Event()
    result: list[object] = []

    def _poll() -> None:
        result.append(reader.poll(10.0))
        returned.set()

    thread = threading.Thread(target=_poll, name="kafka-reader-blocked", daemon=True)
    thread.start()
    deadline = time.monotonic() + POLL_TIMEOUT_SEC * 4
    while not double.partition_reader().calls_of("poll") and time.monotonic() < deadline:
        time.sleep(0.005)

    started = time.monotonic()
    reader.close()

    assert returned.wait(POLL_TIMEOUT_SEC * 4)
    assert time.monotonic() - started < POLL_TIMEOUT_SEC * 4
    assert result == [None]
    thread.join(POLL_TIMEOUT_SEC)


def test_a_reader_whose_anchoring_fails_outside_the_client_hierarchy_still_closes_its_consumer(monkeypatch):
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    build = double.clients.partition_reader

    def _armed(*, auto_offset_reset):
        client = build(auto_offset_reset=auto_offset_reset)
        client.fail_next("seek_to_end", KeyboardInterrupt())
        return client

    monkeypatch.setattr(double.clients, "partition_reader", _armed)

    with pytest.raises(KeyboardInterrupt):
        outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)

    assert [call.kwargs for call in double.partition_reader().calls_of("close")] == [
        {"autocommit": False, "timeout_ms": _CLOSE_TIMEOUT_MS}
    ]
