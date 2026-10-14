from __future__ import annotations

import json

from new_core.coordination.interfaces import TaskStopNotice
from ouroboros.app.task_stop_kafka import KafkaTaskStopConsumer, KafkaTaskStopPublisher
from tests.helpers.kafka_double import KafkaDoubleClientFactory, KafkaDoubleCluster

TOPIC = "ouroboros-test.task-stops"
NOTICE = TaskStopNotice(runtime_id="runtime-a", task_ids=("task-1", "task-2"))


def _fleet() -> tuple[KafkaDoubleCluster, KafkaDoubleClientFactory]:
    cluster = KafkaDoubleCluster()
    cluster.create_topic(TOPIC, partitions=1, configs={})
    return cluster, KafkaDoubleClientFactory(cluster=cluster)


def _consumer(clients: KafkaDoubleClientFactory) -> KafkaTaskStopConsumer:
    return KafkaTaskStopConsumer(clients=clients, topic=TOPIC)


def test_every_runtime_reading_the_topic_sees_the_same_addressed_notice() -> None:
    cluster, clients = _fleet()
    publisher = KafkaTaskStopPublisher(clients=clients, topic=TOPIC)
    first, second = _consumer(clients), _consumer(clients)
    assert [first.start(), second.start()] == [True, True]

    publisher.publish(NOTICE)

    assert [first.poll(0.0), second.poll(0.0)] == [NOTICE, NOTICE]
    assert cluster.end_offset(TOPIC, 0) == 1


def test_a_consumer_anchors_at_the_end_so_a_notice_published_before_it_started_is_not_replayed() -> None:
    _cluster, clients = _fleet()
    publisher = KafkaTaskStopPublisher(clients=clients, topic=TOPIC)
    publisher.publish(TaskStopNotice(runtime_id="runtime-a", task_ids=("before-this-runtime",)))
    consumer = _consumer(clients)

    assert consumer.start() is True

    assert consumer.poll(0.0) is None
    publisher.publish(NOTICE)
    assert consumer.poll(0.0) == NOTICE


def test_a_start_the_broker_refused_replays_what_the_topic_retains() -> None:
    cluster, clients = _fleet()
    publisher = KafkaTaskStopPublisher(clients=clients, topic=TOPIC)
    publisher.publish(NOTICE)
    consumer = _consumer(clients)
    cluster.set_outage(True)

    assert consumer.start() is False

    cluster.set_outage(False)
    assert consumer.poll(0.0) == NOTICE


def test_an_outage_met_while_reading_is_answered_with_none_rather_than_a_raise(monkeypatch) -> None:
    monkeypatch.setattr("ouroboros.app.task_stop_kafka.BROKER_RETRY_SEC", 0.0)
    cluster, clients = _fleet()
    consumer = _consumer(clients)
    assert consumer.start() is True
    cluster.set_outage(True)

    assert consumer.poll(0.0) is None

    cluster.set_outage(False)
    KafkaTaskStopPublisher(clients=clients, topic=TOPIC).publish(NOTICE)
    assert consumer.poll(0.0) == NOTICE


def test_a_record_that_does_not_decode_is_skipped_and_the_next_notice_still_arrives() -> None:
    cluster, clients = _fleet()
    consumer = _consumer(clients)
    assert consumer.start() is True
    cluster.append_raw(TOPIC, 0, b"{not json")
    cluster.append_raw(TOPIC, 0, json.dumps({"version": "9", "runtime_id": "r", "task_ids": "[]"}).encode())
    KafkaTaskStopPublisher(clients=clients, topic=TOPIC).publish(NOTICE)

    assert consumer.poll(0.0) == NOTICE
