import asyncio
import logging
from threading import Event

from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.redis_double import async_client_onto, client_onto, fake_server
from tests.helpers.transport_conformance import full_envelope, message
from transport.infra.redis_gateway import RedisInbox, RedisMessageGateway, RedisOutbox
from transport.interfaces import SubscriptionMode


@parametrize_from_dict(
    {
        "healthy_sweep": {"sweep": "healthy"},
        "failed_sweep": {"sweep": "failed"},
        "stalled_sweep": {"sweep": "stalled"},
    }
)
def test_async_gateway_delivers_and_reports_loss_even_when_subscription_cleanup_fails(monkeypatch, caplog, sweep):
    async def exercise():
        monkeypatch.setattr("transport.infra.redis_gateway._MAINTENANCE_INTERVAL_SEC", 0.0)
        monkeypatch.setattr("transport.infra.redis_gateway._MAX_STREAM_ENTRIES", 2)
        monkeypatch.setattr("transport.infra.redis_gateway.BROKER_SOCKET_TIMEOUT_SEC", 0.05)
        server = fake_server()
        client = client_onto(server)
        async_client = async_client_onto(server)
        gateway = RedisMessageGateway(
            inbox=RedisInbox(client=client, append_client=client, stream="in", group="supervisor", consumer="one"),
            outbox=RedisOutbox(client=client, append_client=client, stream="out"),
            async_client=async_client,
        )
        reader = gateway.outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        orphan = "browser#b#orphan"
        client.xgroup_create("out", orphan, id="0")

        release = Event()

        def invalid_reply(*args, **kwargs):
            if sweep == "failed":
                raise ValueError("invalid EXISTS response")
            assert release.wait(5)
            return 0

        if sweep != "healthy":
            monkeypatch.setitem(client.response_callbacks, "EXISTS", invalid_reply)
            monkeypatch.setitem(async_client.response_callbacks, "EXISTS", invalid_reply)
        try:
            inbound = full_envelope()
            await gateway.asubmit(inbound)
            assert gateway.inbox.poll(0.0) == inbound
            with caplog.at_level(logging.WARNING, logger="transport.infra.redis_gateway"):
                try:
                    async with asyncio.timeout(1):
                        for index in range(4):
                            await gateway.apublish(message(f"m{index}", source="dialogs"))
                finally:
                    release.set()
                    await asyncio.get_running_loop().shutdown_default_executor()
            delivered = reader.poll(0.0)
            assert delivered is not None and delivered.message.text == "m2"
            reader.ack(delivered)
            assert any("lost messages" in record.getMessage() for record in caplog.records)
            assert (orphan in {group["name"] for group in client.xinfo_groups("out")}) == (sweep != "healthy")
        finally:
            reader.close()
            await gateway.aclose()
            client.close()

    asyncio.run(exercise())
