import asyncio

import pytest
from kafka.errors import TopicAuthorizationFailedError

from tests.helpers.kafka_gateway import INBOUND_TOPIC, KafkaGatewayDouble
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE
from transport.infra.kafka_gateway import KafkaMessageGateway
from transport.interfaces import InboundEnvelope, OutboundMessage, SubscriptionMode


@pytest.mark.parametrize("outbound", [False, True])
def test_async_gateway_waits_for_ack_and_uses_one_producer(outbound):
    double = KafkaGatewayDouble()
    gateway = KafkaMessageGateway(inbox=double.inbox(), outbox=double.outbox(), clients=double.clients)

    async def scenario():
        await gateway.start()
        envelope = InboundEnvelope(scope=TEST_SCOPE, text="inbound", user_id="user", trace_id="trace")
        message = OutboundMessage(scope=TEST_SCOPE, source="browser", text="outbound", trace_id="trace")
        reader = gateway.outbox.reader(name="async-payload", mode=SubscriptionMode.BROADCAST)
        await gateway.asubmit(envelope)
        await gateway.apublish(message)
        double.cluster.set_outage(True)
        pending = asyncio.create_task(gateway.apublish(message) if outbound else gateway.asubmit(envelope))
        for _ in range(10):
            await asyncio.sleep(0)
        assert not pending.done()
        double.cluster.set_outage(False)
        await asyncio.wait_for(pending, 2)
        assert gateway.inbox.poll(0) == envelope
        delivered = reader.poll(0)
        assert delivered.message == message
        reader.close()
        await gateway.aclose()
        with pytest.raises(RuntimeError, match="started"):
            await gateway.asubmit(envelope)

    asyncio.run(scenario())
    assert not double.built("producer")
    [producer] = double.built("async_producer")
    assert producer.wrapped.stopped


@parametrize_from_dict(
    {
        "failure": {"failure": "error"},
        "timeout": {"failure": "timeout"},
        "cancellation": {"failure": "cancel"},
    }
)
def test_async_publication_never_reports_success_without_ack(monkeypatch, failure):
    double = KafkaGatewayDouble()
    gateway = KafkaMessageGateway(inbox=double.inbox(), outbox=double.outbox(), clients=double.clients)

    async def scenario():
        await gateway.start()
        envelope = InboundEnvelope(scope=TEST_SCOPE, text="pending", user_id="user")
        if failure == "error":
            double.cluster.reject_next_send(INBOUND_TOPIC, TopicAuthorizationFailedError("broker refused"))
            with pytest.raises(RuntimeError):
                await gateway.asubmit(envelope)
        else:
            double.cluster.set_outage(True)
            if failure == "timeout":
                monkeypatch.setattr("transport.infra.kafka_gateway._SEND_TIMEOUT_SEC", 0)
            task = asyncio.create_task(gateway.asubmit(envelope))
            await asyncio.sleep(0)
            if failure == "cancel":
                task.cancel()
            with pytest.raises(TimeoutError if failure == "timeout" else asyncio.CancelledError):
                await task
            double.cluster.set_outage(False)
        await gateway.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_unsuccessful_start_closes_its_producer(monkeypatch, cancel):
    double = KafkaGatewayDouble()
    gateway = KafkaMessageGateway(inbox=double.inbox(), outbox=double.outbox(), clients=double.clients)
    double.cluster.set_outage(True)
    if not cancel:
        monkeypatch.setattr("transport.infra.kafka_gateway.KAFKA_JOIN_TIMEOUT_SEC", 0)

    async def scenario():
        task = asyncio.create_task(gateway.start())
        await asyncio.sleep(0)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
        assert double.built("async_producer")[0].wrapped.stopped

    asyncio.run(scenario())


def test_cancelled_close_waits_for_bounded_producer_cleanup(monkeypatch):
    double = KafkaGatewayDouble()
    gateway = KafkaMessageGateway(inbox=double.inbox(), outbox=double.outbox(), clients=double.clients)

    async def scenario():
        await gateway.start()
        producer = double.built("async_producer")[0].wrapped
        entered, finish = asyncio.Event(), asyncio.Event()

        async def stop():
            entered.set()
            await finish.wait()
            producer.stopped = True

        monkeypatch.setattr(producer, "stop", stop)
        task = asyncio.create_task(gateway.aclose())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert producer.stopped

    asyncio.run(scenario())


def test_stop_deadline_closes_real_producer_and_settles_unconfirmed_batches(monkeypatch):
    from aiokafka import AIOKafkaProducer, TopicPartition
    from aiokafka.client import AIOKafkaClient

    from infra.kafka_client import KafkaClientFactory
    from tests.helpers.kafka_helpers import kafka_connection
    from tests.helpers.kafka_recording import KafkaRecordingClientFactory

    closed = []
    original_close = AIOKafkaClient.close

    async def start_without_broker(producer):
        producer._txn_manager.set_pid_and_epoch(1, 0)
        await producer._sender.start()

    async def close(client):
        await original_close(client)
        closed.append(client)

    monkeypatch.setattr(AIOKafkaProducer, "start", start_without_broker)
    monkeypatch.setattr(AIOKafkaClient, "close", close)
    monkeypatch.setattr("transport.infra.kafka_gateway.KAFKA_CLOSE_TIMEOUT_MS", 1)
    factory = KafkaClientFactory()
    factory.initialize(kafka_connection("async-stop"))
    clients = KafkaRecordingClientFactory(clients=factory)
    double = KafkaGatewayDouble()
    gateway = KafkaMessageGateway(inbox=double.inbox(), outbox=double.outbox(), clients=clients)

    async def scenario():
        before = asyncio.all_tasks()
        await gateway.start()
        producer = clients.clients_of("async_producer")[0].client.wrapped
        pending = await producer._message_accumulator.add_message(TopicPartition(INBOUND_TOPIC, 0), None, b"payload", 1)
        try:
            with pytest.raises(TimeoutError):
                await gateway.aclose()
            assert producer.client in closed
            assert pending.done() and pending.exception() is not None
            assert producer._sender.sender_task.done()
            await asyncio.sleep(0)
            assert not (asyncio.all_tasks() - before)
        finally:
            if not producer._sender.sender_task.done():
                producer._sender.sender_task.cancel()
            pending.cancel()
            await producer.client.close()

    asyncio.run(scenario())


def test_browser_lifespan_starts_and_closes_the_async_producer(monkeypatch):
    from starlette.testclient import TestClient

    from multiuser_config.transport.browser.app import build_transport_app
    from tests.helpers.kafka_composition import select_kafka
    from tests.helpers.security_context import TEST_CREDENTIALS_KEY
    from transport.browser.ws import reset_browser_ws_state_for_tests

    monkeypatch.setenv("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY", TEST_CREDENTIALS_KEY)
    select_kafka(monkeypatch)
    double = KafkaGatewayDouble()
    gateway = KafkaMessageGateway(inbox=double.inbox(), outbox=double.outbox(), clients=double.clients)
    app = build_transport_app(gateway=gateway)
    assert not double.built("async_producer")
    try:
        with TestClient(app):
            [producer] = double.built("async_producer")
            assert producer.wrapped.started and not producer.wrapped.stopped
        assert producer.wrapped.stopped
    finally:
        reset_browser_ws_state_for_tests()
