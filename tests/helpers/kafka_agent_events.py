from __future__ import annotations

from kafka.partitioner.default import murmur2

from infra.kafka_client import IKafkaClientFactory, KafkaConnection
from new_core.identity import OwnerScope
from ouroboros.agent_events.kafka_transport import (
    KAFKA_AGENT_EVENT_MAX_POLL_INTERVAL_MS,
    KafkaAgentEventConsumer,
    KafkaAgentEventPublisher,
)
from ouroboros.app.broker_composition import AGENT_EVENT_STREAM, INBOX_GROUP
from tests.helpers.kafka_double import KafkaDoubleClientFactory, KafkaDoubleCluster
from tests.helpers.kafka_helpers import TEST_EVENT_TOPIC_PARTITIONS, create_fleet_topics
from tests.helpers.kafka_recording import KafkaRecordingClientFactory, RecordingClient
from tests.helpers.shared import TEST_SCOPE, scope_with

SCOPE_SEARCH_LIMIT = 1_000


def kafka_agent_event_publisher(clients: IKafkaClientFactory, connection: KafkaConnection) -> KafkaAgentEventPublisher:
    return KafkaAgentEventPublisher(clients=clients, topic=connection.topic(AGENT_EVENT_STREAM))


def kafka_agent_event_consumer(
    clients: IKafkaClientFactory, connection: KafkaConnection, *, client_id: str
) -> KafkaAgentEventConsumer:
    return KafkaAgentEventConsumer(
        clients=clients,
        topic=connection.topic(AGENT_EVENT_STREAM),
        group_id=connection.group_id(AGENT_EVENT_STREAM, INBOX_GROUP),
        client_id=client_id,
        max_poll_interval_ms=KAFKA_AGENT_EVENT_MAX_POLL_INTERVAL_MS,
    )


def kafka_agent_event_transport(
    clients: IKafkaClientFactory, connection: KafkaConnection, *, client_id: str
) -> tuple[KafkaAgentEventPublisher, KafkaAgentEventConsumer]:
    return (
        kafka_agent_event_publisher(clients, connection),
        kafka_agent_event_consumer(clients, connection, client_id=client_id),
    )


class KafkaAgentEventRig:
    def __init__(self, *, connection: KafkaConnection) -> None:
        self.connection = connection
        self.cluster = KafkaDoubleCluster()
        create_fleet_topics(self.cluster, connection)
        self.clients = KafkaRecordingClientFactory(clients=KafkaDoubleClientFactory(cluster=self.cluster))

    @property
    def topic(self) -> str:
        return self.connection.topic(AGENT_EVENT_STREAM)

    @property
    def group_id(self) -> str:
        return self.connection.group_id(AGENT_EVENT_STREAM, INBOX_GROUP)

    def publisher(self) -> KafkaAgentEventPublisher:
        return kafka_agent_event_publisher(self.clients, self.connection)

    def consumer(self, client_id: str) -> KafkaAgentEventConsumer:
        return kafka_agent_event_consumer(self.clients, self.connection, client_id=client_id)

    def group_consumer(self, client_id: str) -> RecordingClient:
        built = [
            recorded.client
            for recorded in self.clients.clients_of("group_consumer")
            if recorded.kwargs["client_id"] == client_id
        ]
        if not built:
            raise AssertionError(f"no group consumer was built for {client_id}: poll the consumer first")
        return built[-1]

    def committed(self, scope: OwnerScope) -> int | None:
        return self.cluster.committed(self.group_id, self.topic, self.partition_of(scope))

    def partition_of(self, scope: OwnerScope) -> int:
        key = KafkaAgentEventPublisher.record_key(scope)
        return (murmur2(key) & 0x7FFFFFFF) % TEST_EVENT_TOPIC_PARTITIONS

    def scope_on(self, partition: int) -> OwnerScope:
        for index in range(SCOPE_SEARCH_LIMIT):
            scope = scope_with(TEST_SCOPE, chat_id=f"chat-{index}")
            if self.partition_of(scope) == partition:
                return scope
        raise AssertionError(f"no scope maps to partition {partition} among {SCOPE_SEARCH_LIMIT} chats")


__all__ = [
    "KafkaAgentEventRig",
    "kafka_agent_event_consumer",
    "kafka_agent_event_publisher",
    "kafka_agent_event_transport",
]
