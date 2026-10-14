from __future__ import annotations

import queue
import threading
import time

import redis

from new_core.coordination.interfaces import TaskStopNotice
from ouroboros.app.task_stop_notifications import (
    InProcessTaskStopConsumer,
    InProcessTaskStopPublisher,
    RedisTaskStopConsumer,
    RedisTaskStopPublisher,
)
from tests.helpers.redis_double import client_onto, fake_server


def test_the_in_process_transport_carries_a_typed_notice() -> None:
    messages = queue.Queue()
    publisher = InProcessTaskStopPublisher(messages)
    consumer = InProcessTaskStopConsumer(messages)
    notice = TaskStopNotice(runtime_id="runtime-a", task_ids=("task-1", "task-2"))

    publisher.publish(notice)

    assert consumer.poll(0.0) == notice
    assert consumer.poll(0.0) is None


def test_redis_stream_consumers_each_see_the_same_addressed_notice() -> None:
    server = fake_server()
    publisher = RedisTaskStopPublisher(client_onto(server), stream="test:task-stops")
    first = RedisTaskStopConsumer(client_onto(server), stream="test:task-stops")
    second = RedisTaskStopConsumer(client_onto(server), stream="test:task-stops")
    notice = TaskStopNotice(runtime_id="runtime-a", task_ids=("task-1",))
    first.start()
    second.start()
    received: list[TaskStopNotice | None] = []
    readers = [
        threading.Thread(target=lambda consumer=consumer: received.append(consumer.poll(1.0)))
        for consumer in (first, second)
    ]
    for reader in readers:
        reader.start()
    time.sleep(0.02)

    publisher.publish(notice)
    for reader in readers:
        reader.join(2)

    assert all(not reader.is_alive() for reader in readers)
    assert received == [notice, notice]


def test_a_blocking_consumer_wakes_when_a_notice_arrives() -> None:
    server = fake_server()
    publisher = RedisTaskStopPublisher(client_onto(server), stream="test:task-stops")
    consumer = RedisTaskStopConsumer(client_onto(server), stream="test:task-stops")
    consumer.start()
    received: list[TaskStopNotice | None] = []
    thread = threading.Thread(target=lambda: received.append(consumer.poll(1.0)))
    thread.start()
    time.sleep(0.02)

    publisher.publish(TaskStopNotice(runtime_id="runtime-a", task_ids=("task-1",)))
    thread.join(2)

    assert not thread.is_alive()
    assert received == [TaskStopNotice(runtime_id="runtime-a", task_ids=("task-1",))]


def test_redis_consumer_anchors_before_a_notice_can_be_published() -> None:
    """Startup readiness closes the old publish-before-first-poll loss window."""
    server = fake_server()
    publisher = RedisTaskStopPublisher(client_onto(server), stream="test:task-stops")
    consumer = RedisTaskStopConsumer(client_onto(server), stream="test:task-stops")
    notice = TaskStopNotice(runtime_id="runtime-a", task_ids=("task-1",))

    consumer.start()
    publisher.publish(notice)

    assert consumer.poll(0.0) == notice


def test_redis_consumer_replays_retained_notices_after_startup_failure(monkeypatch) -> None:
    """A runtime allowed to claim on broker failure must not later anchor past its notices."""
    server = fake_server()
    client = client_onto(server)
    publisher = RedisTaskStopPublisher(client_onto(server), stream="test:task-stops")
    consumer = RedisTaskStopConsumer(client, stream="test:task-stops")
    xrevrange = client.xrevrange
    monkeypatch.setattr(client, "xrevrange", lambda *_args, **_kwargs: (_ for _ in ()).throw(redis.RedisError()))

    assert consumer.start() is False
    publisher.publish(TaskStopNotice(runtime_id="runtime-a", task_ids=("task-1",)))
    monkeypatch.setattr(client, "xrevrange", xrevrange)

    assert consumer.poll(0.0) == TaskStopNotice(runtime_id="runtime-a", task_ids=("task-1",))
