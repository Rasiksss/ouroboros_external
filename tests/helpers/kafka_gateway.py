from __future__ import annotations

from tests.helpers.kafka_double import KafkaDoubleClientFactory, KafkaDoubleCluster
from tests.helpers.kafka_recording import KafkaRecordingClientFactory, RecordingClient
from transport.infra.kafka_gateway import KAFKA_INBOX_MAX_POLL_INTERVAL_MS, KafkaInbox, KafkaOutbox

INBOUND_TOPIC = "ouroboros-test.inbound"
OUTBOUND_TOPIC = "ouroboros-test.outbound"
INBOX_GROUP_ID = "ouroboros-test.inbound.supervisor"
INBOX_CLIENT_ID = "supervisor-1"


class KafkaGatewayDouble:
    def __init__(self, *, inbound_partitions: int = 1, outbound_partitions: int = 1) -> None:
        self.cluster = KafkaDoubleCluster()
        self.cluster.create_topic(INBOUND_TOPIC, partitions=inbound_partitions, configs={})
        self.cluster.create_topic(OUTBOUND_TOPIC, partitions=outbound_partitions, configs={})
        self.clients = KafkaRecordingClientFactory(clients=KafkaDoubleClientFactory(cluster=self.cluster))

    def inbox(self) -> KafkaInbox:
        return KafkaInbox(
            clients=self.clients,
            topic=INBOUND_TOPIC,
            group_id=INBOX_GROUP_ID,
            client_id=INBOX_CLIENT_ID,
            max_poll_interval_ms=KAFKA_INBOX_MAX_POLL_INTERVAL_MS,
        )

    def outbox(self) -> KafkaOutbox:
        return KafkaOutbox(clients=self.clients, topic=OUTBOUND_TOPIC)

    def built(self, accessor: str) -> list[RecordingClient]:
        return [recorded.client for recorded in self.clients.clients_of(accessor)]

    def group_consumer(self) -> RecordingClient:
        consumers = self.built("group_consumer")
        assert len(consumers) == 1, f"expected one group consumer, found {len(consumers)}"
        return consumers[0]

    def partition_reader(self) -> RecordingClient:
        readers = self.built("partition_reader")
        assert readers, "no partition reader was built"
        return readers[-1]


__all__ = [
    "INBOUND_TOPIC",
    "INBOX_CLIENT_ID",
    "INBOX_GROUP_ID",
    "OUTBOUND_TOPIC",
    "KafkaGatewayDouble",
]
