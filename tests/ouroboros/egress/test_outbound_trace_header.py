"""Every outbound HTTP integration carries the active operation identifier, per request.

The clients here outlive one operation, so the header cannot be merged in at construction: it
would attribute every later operation on the connection to whichever one built it. The hook
reads the context variable at send time instead.

The MCP and outbound-A2A clients live on the app's lifespan loop and are reached across the
cross-loop bridge. `run_coroutine_threadsafe` propagates the caller's context to the task it
schedules, so the identifier is readable there; `tests/new_core/test_loop_bridge.py` pins that
guarantee, and the bridged case here is its end-to-end consequence.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from new_core.tools.loop_bridge import call_on_loop_sync
from ouroboros.egress.a2a.transports import build_http_client
from ouroboros.observability import aef_tracing
from ouroboros.storage.settings.schema import A2aRemoteAgentEntry

DASHED = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
#: The other spelling a caller may send. It is normalized to the dashed form, which is the one
#: spelling the agent stores, logs and puts on the wire.
PACKED = "f47ac10b58cc4372a5670e02b2c3d479"
OTHER = "463ac35c-9f64-13ad-4848-5a3953bb6124"


def _recording_transport(seen: list[str | None]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(aef_tracing.TRACE_HEADER_NAME))
        return httpx.Response(200, json={})

    return httpx.MockTransport(handle)


def _entry() -> A2aRemoteAgentEntry:
    return A2aRemoteAgentEntry(name="remote", url="http://remote.test/")


def test_the_outbound_a2a_client_sends_the_active_identifier():
    seen: list[str | None] = []
    client = build_http_client(_entry(), frozenset())
    client._transport = _recording_transport(seen)

    async def send() -> None:
        with aef_tracing.trace_scope(DASHED):
            await client.get("http://remote.test/.well-known/agent-card.json")
        await client.aclose()

    asyncio.run(send())

    assert seen == [DASHED]


def test_two_operations_over_one_connection_each_send_their_own_identifier():
    seen: list[str | None] = []
    client = build_http_client(_entry(), frozenset())
    client._transport = _recording_transport(seen)

    async def send_twice() -> None:
        for trace_id in (DASHED, OTHER):
            with aef_tracing.trace_scope(trace_id):
                await client.get("http://remote.test/")
        await client.aclose()

    asyncio.run(send_twice())

    assert seen == [DASHED, OTHER]


def test_an_outbound_call_outside_an_operation_omits_the_header():
    seen: list[str | None] = []
    client = build_http_client(_entry(), frozenset())
    client._transport = _recording_transport(seen)

    async def send() -> None:
        await client.get("http://remote.test/")
        await client.aclose()

    assert aef_tracing.current_trace_id() == ""
    asyncio.run(send())

    assert seen == [None]


@pytest.fixture
def lifespan_loop():
    """A loop on its own thread, as the app's lifespan loop is to a tool body on a worker."""
    import threading

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_a_client_on_the_lifespan_loop_sends_the_bridged_identifier(lifespan_loop):
    seen: list[str | None] = []
    client = build_http_client(_entry(), frozenset())
    client._transport = _recording_transport(seen)

    def send() -> object:
        return client.get("http://remote.test/")

    with aef_tracing.trace_scope(DASHED):
        call_on_loop_sync(lifespan_loop, send, timeout_sec=5)

    asyncio.run_coroutine_threadsafe(client.aclose(), lifespan_loop).result(timeout=5)

    assert seen == [DASHED]


def test_a_repeated_header_on_the_request_survives_the_stamp():
    """The hook assigns one key rather than rewriting the header set.

    `httpx.Headers.items()` comma-joins a repeated name, so reading the headers out and putting
    them back collapses every legitimately duplicated header on the request into one. A2A sends
    `X-A2A-Extensions` that way, and a collapsed extension list is a protocol change, not a
    cosmetic one.
    """
    seen: list[list[str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append([value for name, value in request.headers.multi_items() if name.lower() == "x-a2a-extensions"])
        return httpx.Response(200, json={})

    async def send() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            event_hooks=aef_tracing.outbound_request_event_hooks(),
        ) as client:
            with aef_tracing.trace_scope(DASHED):
                await client.get(
                    "http://remote.test/",
                    headers=[("X-A2A-Extensions", "one"), ("X-A2A-Extensions", "two")],
                )

    asyncio.run(send())

    assert seen == [["one", "two"]]


def test_the_outbound_a2a_client_says_which_agent_is_calling(monkeypatch):
    from ouroboros.storage.settings import store

    monkeypatch.setattr(
        store,
        "load_settings",
        lambda: SimpleNamespace(AGENT_SYSTEM_CONFIGURATION_AGENT_ID="CI0000123", AGENT_ID=""),
    )
    seen: list[tuple[str | None, str | None]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.headers.get(aef_tracing.TRACE_HEADER_NAME),
                request.headers.get(aef_tracing.AGENT_HEADER_NAME),
            )
        )
        return httpx.Response(200, json={})

    client = build_http_client(_entry(), frozenset())
    client._transport = httpx.MockTransport(handle)

    async def send() -> None:
        with aef_tracing.trace_scope(DASHED):
            await client.get("http://remote.test/.well-known/agent-card.json")
        await client.aclose()

    asyncio.run(send())

    assert seen == [(DASHED, "CI0000123")]
