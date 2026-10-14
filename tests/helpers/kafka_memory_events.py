from __future__ import annotations

import time
from collections.abc import Sequence

from kafka.partitioner.default import DefaultPartitioner

from background_memory.infra.events.kafka_transport import (
    KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS,
    KafkaMemoryEventConsumer,
    KafkaMemoryEventPublisher,
)
from background_memory.interfaces import IMemoryEventConsumer
from infra.kafka_client import IKafkaClientFactory, KafkaConnection
from new_core.identity import OwnerScope
from ouroboros.app.broker_composition import MEMORY_EVENT_GROUP, MEMORY_EVENT_STREAM
from tests.helpers.kafka_double import KafkaDoubleClientFactory, KafkaDoubleCluster
from tests.helpers.kafka_helpers import (
    ASSIGNMENT_TIMEOUT_SEC,
    TEST_EVENT_TOPIC_PARTITIONS,
    create_fleet_topics,
    fresh_prefix,
    kafka_connection,
)
from tests.helpers.kafka_recording import KafkaRecordingClientFactory
from tests.helpers.memory_event_conformance import (
    DELIVERY_TIMEOUT_SEC,
    QUIET_POLL_SEC,
    distillation_event,
    poll,
    task_id_of,
)
from tests.helpers.shared import TEST_SCOPE, scope_with

MODULE = "background_memory.infra.events.kafka_transport"
SCOPE_SEARCH_LIMIT = 1_000
CONNECTION = kafka_connection(fresh_prefix())


def memory_topic(connection: KafkaConnection) -> str:
    return connection.topic(MEMORY_EVENT_STREAM)


def memory_group(connection: KafkaConnection) -> str:
    return connection.group_id(MEMORY_EVENT_STREAM, MEMORY_EVENT_GROUP)


def fleet_double() -> tuple[KafkaDoubleCluster, KafkaRecordingClientFactory]:
    cluster = KafkaDoubleCluster()
    create_fleet_topics(cluster, CONNECTION)
    return cluster, KafkaRecordingClientFactory(clients=KafkaDoubleClientFactory(cluster=cluster))


def memory_publisher(clients: IKafkaClientFactory, connection: KafkaConnection) -> KafkaMemoryEventPublisher:
    return KafkaMemoryEventPublisher(clients=clients, topic=memory_topic(connection))


def memory_consumer(
    clients: IKafkaClientFactory,
    connection: KafkaConnection,
    *,
    client_id: str,
    max_poll_interval_ms: int = KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS,
) -> KafkaMemoryEventConsumer:
    return KafkaMemoryEventConsumer(
        clients=clients,
        topic=memory_topic(connection),
        group_id=memory_group(connection),
        client_id=client_id,
        max_poll_interval_ms=max_poll_interval_ms,
    )


def partition_of(scope: OwnerScope) -> int:
    partitions = list(range(TEST_EVENT_TOPIC_PARTITIONS))
    return DefaultPartitioner()(KafkaMemoryEventPublisher.record_key(scope), partitions, partitions)


def scope_on(partition: int) -> OwnerScope:
    for index in range(SCOPE_SEARCH_LIMIT):
        scope = scope_with(TEST_SCOPE, owner_id=f"owner-{index}")
        if partition_of(scope) == partition:
            return scope
    raise AssertionError(f"no owner maps to partition {partition} among {SCOPE_SEARCH_LIMIT} owners")


def start_consumers(clients: KafkaRecordingClientFactory, readers: Sequence[KafkaMemoryEventConsumer]) -> None:
    client_ids = {reader._client_id for reader in readers}
    deadline = time.monotonic() + ASSIGNMENT_TIMEOUT_SEC
    while True:
        assert [poll(reader, QUIET_POLL_SEC) for reader in readers] == [None] * len(readers)
        members = [
            recorded.client.wrapped
            for recorded in clients.clients_of("async_group_consumer")
            if recorded.kwargs["client_id"] in client_ids
        ]
        if len(members) == len(readers) and all(member.assignment() for member in members):
            break
        assert time.monotonic() < deadline, f"{sorted(client_ids)} did not all hold an assignment in time"
    assert [poll(reader, QUIET_POLL_SEC) for reader in readers] == [None] * len(readers)


def record_every_position(publisher: KafkaMemoryEventPublisher, readers: Sequence[IMemoryEventConsumer]) -> None:
    published = [f"warm-up-{partition}" for partition in range(TEST_EVENT_TOPIC_PARTITIONS)]
    for partition, task_id in enumerate(published):
        publisher.publish(distillation_event(task_id, scope_on(partition)))
    delivered: list[str | None] = []
    deadline = time.monotonic() + ASSIGNMENT_TIMEOUT_SEC
    while len(delivered) < len(published) and time.monotonic() < deadline:
        for reader in readers:
            polled = poll(reader, QUIET_POLL_SEC)
            if polled is not None:
                delivered.append(task_id_of(polled))
    assert sorted(str(task_id) for task_id in delivered) == published


def delivered_task_ids(
    consumer: IMemoryEventConsumer, first_wait_sec: float = DELIVERY_TIMEOUT_SEC
) -> list[str | None]:
    delivered: list[str | None] = []
    while (polled := poll(consumer, first_wait_sec if not delivered else QUIET_POLL_SEC)) is not None:
        delivered.append(task_id_of(polled))
    return delivered


__all__ = [
    "CONNECTION",
    "MODULE",
    "delivered_task_ids",
    "fleet_double",
    "memory_consumer",
    "memory_group",
    "memory_publisher",
    "memory_topic",
    "partition_of",
    "record_every_position",
    "scope_on",
    "start_consumers",
]
