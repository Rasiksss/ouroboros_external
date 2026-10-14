from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from kafka.errors import TopicAuthorizationFailedError

from ouroboros.app.broker_composition import INBOUND_STREAM, INBOX_GROUP, OUTBOUND_STREAM
from tests.helpers.kafka_gateway import INBOUND_TOPIC, INBOX_GROUP_ID, OUTBOUND_TOPIC, KafkaGatewayDouble
from tests.helpers.kafka_helpers import (
    await_assignment,
    create_broker_topics,
    fleet_topics,
    fresh_prefix,
    needs_kafka,
    real_client_factory,
    release_real_broker,
)
from tests.helpers.kafka_recording import KafkaRecordingClientFactory, RecordingClient
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.transport_conformance import InboxConformance, OutboxConformance
from transport.infra.kafka_gateway import (
    _CLOSE_TIMEOUT_MS,
    KAFKA_INBOX_MAX_POLL_INTERVAL_MS,
    KafkaInbox,
    KafkaMessageGateway,
    KafkaOutbox,
)
from transport.interfaces import IInbox, IMessageGateway, InboundEnvelope, IOutbox, OutboundMessage, SubscriptionMode


class _KafkaDouble:
    def double(self) -> KafkaGatewayDouble:
        if not hasattr(self, "_double"):
            self._double = KafkaGatewayDouble()
        return self._double

    def clients(self) -> KafkaRecordingClientFactory:
        return self.double().clients

    def inbound_topic(self) -> str:
        return INBOUND_TOPIC

    def outbound_topic(self) -> str:
        return OUTBOUND_TOPIC

    def inbox_group_id(self) -> str:
        return INBOX_GROUP_ID

    def ready(self, inbox: KafkaInbox) -> KafkaInbox:
        return inbox

    def depart(self, consumer: RecordingClient) -> None:
        self.double().cluster.crash(consumer.wrapped)

    def break_publish(self, outbox: Any) -> bool:
        del outbox
        self.double().cluster.reject_next_send(OUTBOUND_TOPIC, TopicAuthorizationFailedError)
        return True

    def shrink_retention(self, outbox: Any, monkeypatch: pytest.MonkeyPatch) -> int:
        del outbox, monkeypatch
        self.double().cluster.retain_last(OUTBOUND_TOPIC, 3)
        return 3


class _RealBroker:
    pytestmark = needs_kafka

    @pytest.fixture(autouse=True)
    def _fleet_topics(self):
        self.prefix = fresh_prefix()
        self.factory = real_client_factory(self.prefix)
        self.recording = KafkaRecordingClientFactory(clients=self.factory)
        create_broker_topics(fleet_topics(self.factory.connection))
        yield
        release_real_broker(self.recording, self.prefix)

    def clients(self) -> KafkaRecordingClientFactory:
        return self.recording

    def inbound_topic(self) -> str:
        return self.factory.connection.topic(INBOUND_STREAM)

    def outbound_topic(self) -> str:
        return self.factory.connection.topic(OUTBOUND_STREAM)

    def inbox_group_id(self) -> str:
        return self.factory.connection.group_id(INBOUND_STREAM, INBOX_GROUP)

    def ready(self, inbox: KafkaInbox) -> KafkaInbox:
        await_assignment(inbox, clients=self.recording)
        return inbox

    def depart(self, consumer: RecordingClient) -> None:
        consumer.close(autocommit=False, timeout_ms=_CLOSE_TIMEOUT_MS)

    def break_publish(self, outbox: Any) -> bool:
        del outbox
        pytest.skip(
            "a real broker cannot refuse one append without also refusing the readers' connections; "
            "the Kafka double covers the failed append in the default gate"
        )

    def shrink_retention(self, outbox: Any, monkeypatch: pytest.MonkeyPatch) -> int | None:
        del outbox, monkeypatch
        pytest.skip(
            "a real broker's retention is segment time and size and cannot be shrunk to a count; "
            "the Kafka double covers the drop in the default gate"
        )


class _KafkaInboxConformance(InboxConformance):
    def make_inbox(self) -> KafkaInbox:
        self._inbox_arguments = {
            "clients": self.clients(),
            "topic": self.inbound_topic(),
            "group_id": self.inbox_group_id(),
            "client_id": f"supervisor-{uuid.uuid4().hex}",
            "max_poll_interval_ms": KAFKA_INBOX_MAX_POLL_INTERVAL_MS,
        }
        return self.ready(KafkaInbox(**self._inbox_arguments))

    def replacement_consumer(self, inbox: KafkaInbox) -> KafkaInbox:
        del inbox
        self.depart(self.clients().clients_of("group_consumer")[-1].client)
        return KafkaInbox(**self._inbox_arguments)


class _KafkaOutboxConformance(OutboxConformance):
    def make_outbox(self) -> KafkaOutbox:
        return KafkaOutbox(clients=self.clients(), topic=self.outbound_topic())

    def supports_shared_subscriptions(self) -> bool:
        return False


class TestKafkaDoubleInbox(_KafkaDouble, _KafkaInboxConformance):
    pass


class TestKafkaDoubleOutbox(_KafkaDouble, _KafkaOutboxConformance):
    pass


class TestKafkaInbox(_RealBroker, _KafkaInboxConformance):
    pass


class TestKafkaOutbox(_RealBroker, _KafkaOutboxConformance):
    pass


class TestKafkaAsyncPublication(_RealBroker):
    def test_async_producer_preserves_payload_and_partition_order(self):
        inbox = self.ready(
            KafkaInbox(
                clients=self.clients(),
                topic=self.inbound_topic(),
                group_id=self.inbox_group_id(),
                client_id="async-publication",
                max_poll_interval_ms=KAFKA_INBOX_MAX_POLL_INTERVAL_MS,
            )
        )
        outbox = KafkaOutbox(clients=self.clients(), topic=self.outbound_topic())
        reader = outbox.reader(name="async-publication", mode=SubscriptionMode.BROADCAST)
        gateway = KafkaMessageGateway(inbox=inbox, outbox=outbox, clients=self.clients())
        envelopes = [InboundEnvelope(scope=TEST_SCOPE, text=str(i), user_id="user", trace_id="trace") for i in range(3)]
        messages = [
            OutboundMessage(scope=TEST_SCOPE, source="browser", text=str(i), trace_id="trace") for i in range(3)
        ]

        async def publish():
            await gateway.start()
            try:
                for envelope, message in zip(envelopes, messages, strict=True):
                    await gateway.asubmit(envelope)
                    await gateway.apublish(message)
            finally:
                await gateway.aclose()

        try:
            asyncio.run(publish())
            assert [inbox.poll(5) for _ in envelopes] == envelopes
            for message in messages:
                delivered = reader.poll(5)
                assert delivered is not None and delivered.message == message
                reader.ack(delivered)
        finally:
            reader.close()


def test_the_kafka_gateway_implements_the_seam():
    double = KafkaGatewayDouble()

    gateway = KafkaMessageGateway(inbox=double.inbox(), outbox=double.outbox(), clients=double.clients)

    assert isinstance(gateway, IMessageGateway)
    assert isinstance(gateway.inbox, IInbox)
    assert isinstance(gateway.outbox, IOutbox)
    assert double.clients.built == []
