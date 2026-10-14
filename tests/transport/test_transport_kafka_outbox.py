from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from kafka.errors import KafkaTimeoutError

from tests.helpers.kafka_gateway import OUTBOUND_TOPIC, KafkaGatewayDouble
from tests.helpers.transport_conformance import POLL_TIMEOUT_SEC, message
from transport.interfaces import SubscriptionMode

BUDGET_SEC = 0.15
BUDGET_BOUND_SEC = 0.3
SEND_BOUND_SEC = 0.2
OUTBOUND_PARTITIONS = 4
CONSTANT_BUDGET_SEC = 0.4
METADATA_DELAY_SEC = 0.25


def _budget(seconds: float) -> Callable[[], float]:
    deadline = time.monotonic() + seconds

    def remaining() -> float:
        return deadline - time.monotonic()

    return remaining


def _sends(double: KafkaGatewayDouble) -> list[str]:
    return [call.method for producer in double.built("producer") for call in producer.calls_of("send")]


def _expired_by_raising() -> float:
    raise TimeoutError("authority expired")


def _expired_by_answering_zero() -> float:
    return 0.0


@pytest.mark.parametrize(
    "guard", [_expired_by_raising, _expired_by_answering_zero], ids=["the guard raises", "the guard answers zero"]
)
def test_an_authority_expired_on_the_first_check_issues_no_send(guard):
    double = KafkaGatewayDouble()
    outbox = double.outbox()

    with pytest.raises(TimeoutError):
        outbox.publish(message("never handed over"), before_publish=guard)

    assert _sends(double) == []
    assert double.cluster.end_offset(OUTBOUND_TOPIC, 0) == 0


def test_an_authority_that_expires_after_the_metadata_step_issues_no_send():
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    answers = iter([1.0, 0.0])

    with pytest.raises(TimeoutError, match="deadline expired"):
        outbox.publish(message("expired while the metadata was fetched"), before_publish=lambda: next(answers))

    assert [call.method for call in double.built("producer")[0].calls] == ["partitions_for"]
    assert double.cluster.end_offset(OUTBOUND_TOPIC, 0) == 0


def test_a_budgeted_publish_on_a_warm_producer_during_an_outage_raises_within_its_budget():
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    outbox.publish(message("warms the producer's metadata"))
    double.cluster.set_outage(True)

    started = time.monotonic()
    with pytest.raises(KafkaTimeoutError):
        outbox.publish(message("during the outage"), before_publish=_budget(BUDGET_SEC))
    elapsed = time.monotonic() - started
    double.cluster.set_outage(False)

    assert elapsed < BUDGET_BOUND_SEC


def test_a_healthy_budgeted_publish_returns_within_its_budget_and_builds_only_the_producer():
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    built_before = len(double.clients.built)

    started = time.monotonic()
    outbox.publish(message("accepted"), before_publish=_budget(BUDGET_SEC))
    elapsed = time.monotonic() - started

    assert elapsed < BUDGET_BOUND_SEC
    assert {recorded.accessor for recorded in double.clients.built[built_before:]} == {"producer"}
    assert double.cluster.end_offset(OUTBOUND_TOPIC, 0) == 1


@pytest.mark.parametrize("budgeted", [False, True], ids=["unbudgeted", "budgeted"])
def test_an_outbound_topic_with_several_partitions_delivers_every_message_in_order_to_a_broadcast_reader(budgeted):
    double = KafkaGatewayDouble(outbound_partitions=OUTBOUND_PARTITIONS)
    outbox = double.outbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    published = [f"m{index}" for index in range(8)]
    delivered: list[str] = []
    try:
        for text in published:
            outbox.publish(message(text), before_publish=_budget(5.0) if budgeted else None)
        while (item := reader.poll(POLL_TIMEOUT_SEC)) is not None:
            delivered.append(item.message.text)
            reader.ack(item)
    finally:
        reader.close()

    assert delivered == published
    assert [double.cluster.end_offset(OUTBOUND_TOPIC, partition) for partition in range(OUTBOUND_PARTITIONS)] == [
        len(published),
        0,
        0,
        0,
    ]


@pytest.mark.parametrize("budgeted", [False, True], ids=["unbudgeted", "budgeted"])
def test_a_publish_whose_wait_expired_may_still_land_as_declared(budgeted, monkeypatch):
    monkeypatch.setattr("transport.infra.kafka_gateway._SEND_TIMEOUT_SEC", SEND_BOUND_SEC)
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        outbox.publish(message("warms the producer's metadata"))
        warm = reader.poll(POLL_TIMEOUT_SEC)
        assert warm is not None
        reader.ack(warm)
        double.cluster.set_outage(True)

        with pytest.raises(KafkaTimeoutError):
            outbox.publish(message("lands late"), before_publish=_budget(5.0) if budgeted else None)
        double.cluster.set_outage(False)

        landed = reader.poll(POLL_TIMEOUT_SEC)
        assert landed is not None
        assert landed.message.text == "lands late"
        reader.ack(landed)
    finally:
        reader.close()


def test_the_metadata_step_is_charged_to_the_budget_the_first_check_answered(monkeypatch):
    double = KafkaGatewayDouble()
    outbox = double.outbox()
    outbox.publish(message("warms the producer's metadata"))
    producer = double.built("producer")[0]
    monkeypatch.setattr(producer, "partitions_for", lambda topic: time.sleep(METADATA_DELAY_SEC))
    double.cluster.set_outage(True)

    started = time.monotonic()
    with pytest.raises(KafkaTimeoutError):
        outbox.publish(message("during the outage"), before_publish=lambda: CONSTANT_BUDGET_SEC)
    elapsed = time.monotonic() - started
    double.cluster.set_outage(False)

    assert METADATA_DELAY_SEC <= elapsed < CONSTANT_BUDGET_SEC + METADATA_DELAY_SEC / 2
