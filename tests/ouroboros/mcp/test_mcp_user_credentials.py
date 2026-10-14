from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx2
import pytest

import ouroboros.mcp.connection as connection_mod
import ouroboros.mcp.transports as transports_mod
from infra.security_context.interface import ISecurityContextStore, SecurityParameterKey, SecurityParameterType
from new_core.identity import OwnerScope, SurfaceId
from new_core.tools.loop_bridge import call_on_loop_sync
from new_core.tools.providers.mcp import McpToolProvider
from ouroboros.agent.core import _task_scope
from ouroboros.agent_events.codec import decode_agent_event, encode_agent_event
from ouroboros.agent_events.interfaces import SubtaskRequested
from ouroboros.egress.auth.interfaces import EgressAuthDependencies
from ouroboros.mcp.connection import MCPServerConnection
from ouroboros.mcp.manager import MCPManager
from ouroboros.mcp.transports import open_transport
from ouroboros.observability import aef_tracing
from ouroboros.storage.settings.schema import McpServerEntry
from tests.helpers.mcp_auth import McpPeer, McpPeerRequest, drive_mcp_peer, mock_mcp_client
from tests.helpers.security_context import FixedSecurityContextStore, context_of
from tests.helpers.shared import make_tool_ctx
from tests.helpers.user_settings import StubMcpServerIdsReader
from tests.helpers.user_tokens import unread_user_token_store

ALICE = OwnerScope(owner_id="alice", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="1")
BOB = OwnerScope(owner_id="bob", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="1")
TOKEN_HEADER = "X-Sudir-Token"
CALLER = "1c8f6c0a-2c2f-4f39-88a2-6a6f1b2f4c11"
AGENT_ID = "agent-under-test"
ENTRY = McpServerEntry(
    id="sudir",
    url="https://mcp.test/mcp",
    auth={"kind": "user_headers", "headers": [TOKEN_HEADER], "cookies": ["sudir"]},
)


def _dependencies(store: ISecurityContextStore) -> EgressAuthDependencies:
    return EgressAuthDependencies(security_context_store=store, user_token_store=unread_user_token_store())


def _store() -> FixedSecurityContextStore:
    return FixedSecurityContextStore(
        {
            ALICE.security_token: context_of(
                headers={"x-sudir-token": "alice-token", "authorization": "Bearer alice-undeclared"},
                cookies={"sudir": "alice-cookie", "theme": "dark"},
            ),
            BOB.security_token: context_of(headers={"x-sudir-token": "bob-token"}, cookies={"sudir": "bob-cookie"}),
        }
    )


@asynccontextmanager
async def _connected(store: ISecurityContextStore | None = None, entry: McpServerEntry = ENTRY):
    connection = MCPServerConnection(entry, _dependencies(store if store is not None else _store()))
    await connection.connect()
    try:
        yield connection
    finally:
        await connection.aclose()


def _credentialed(peer: McpPeer) -> list[McpPeerRequest]:
    return peer.carrying(TOKEN_HEADER, "cookie")


async def _call(connection, scope, *, tool: str = "echo", who: str = "", timeout: float = 5) -> str:
    with aef_tracing.trace_scope(CALLER):
        return await connection.call_tool(scope, tool, {"who": who}, timeout)


@pytest.mark.parametrize("era", ["handshake", "modern"])
def test_a_tool_call_carries_the_declared_credentials_and_discovery_carries_none(era, monkeypatch) -> None:
    monkeypatch.setattr(connection_mod, "_HEALTHCHECK_INTERVAL_SEC", 0.05)
    peer = McpPeer(era=era)

    async def body() -> None:
        async with _connected() as connection:
            assert await _call(connection, ALICE, who="a") == "pong:a"
            if era == "handshake":
                peer.pinged.clear()
                await asyncio.wait_for(peer.pinged.wait(), timeout=5)

    drive_mcp_peer(peer, body)

    (call,) = peer.of("tools/call")
    assert call.headers[TOKEN_HEADER] == "alice-token"
    assert call.headers["cookie"] == "sudir=alice-cookie"
    assert "Bearer alice-undeclared" not in call.headers.get("authorization", "")
    assert _credentialed(peer) == [call]
    connection_methods = {sent.method for sent in peer.sent if sent is not call}
    assert {"server/discover", "tools/list"} <= connection_methods
    if era == "handshake":
        assert {"initialize", "notifications/initialized", "ping"} <= connection_methods


def test_trace_and_agent_headers_still_ride_on_a_credentialed_call(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_ID", AGENT_ID)
    peer = McpPeer()

    async def body() -> None:
        async with _connected() as connection:
            await _call(connection, ALICE)

    drive_mcp_peer(peer, body)

    (call,) = peer.of("tools/call")
    assert call.headers[aef_tracing.TRACE_HEADER_NAME] == CALLER
    assert call.headers[aef_tracing.AGENT_HEADER_NAME] == AGENT_ID
    assert call.headers[TOKEN_HEADER] == "alice-token"


def test_the_cancellation_of_a_timed_out_call_carries_no_credentials() -> None:
    peer = McpPeer()
    released = asyncio.Event()

    async def never_answer(sent: McpPeerRequest) -> httpx2.Response | None:
        await released.wait()
        return None

    peer.on_call = never_answer

    async def body() -> None:
        async with _connected() as connection:
            result = await _call(connection, ALICE, timeout=0.3)
            assert result.startswith("⚠️ MCP_TOOL_ERROR (echo)")
            await asyncio.wait_for(peer.cancelled.wait(), timeout=5)
            released.set()

    drive_mcp_peer(peer, body)

    (cancelled,) = peer.of("notifications/cancelled")
    assert TOKEN_HEADER not in cancelled.headers
    assert "cookie" not in cancelled.headers
    assert [sent.method for sent in _credentialed(peer)] == ["tools/call"]


def test_a_call_during_a_reconnects_listing_waits_and_sends_no_credentialed_listing(monkeypatch) -> None:
    monkeypatch.setattr(connection_mod, "_RECONNECT_MIN_DELAY_SEC", 0.01)
    monkeypatch.setattr(connection_mod, "_RECONNECT_WAIT_SEC", 0.05)
    peer = McpPeer()
    listing_started = asyncio.Event()
    release_listing = asyncio.Event()
    order: list[str] = []

    async def body() -> None:
        async with _connected() as connection:

            async def delayed_second_listing() -> None:
                listing_started.set()
                await release_listing.wait()
                order.append("listing answered")

            peer.on_list = delayed_second_listing
            connection._drop_session()
            await asyncio.wait_for(listing_started.wait(), timeout=5)
            call = asyncio.create_task(_call(connection, ALICE, who="a"))
            await asyncio.sleep(0.2)
            assert not call.done()
            order.append("released")
            release_listing.set()
            assert await asyncio.wait_for(call, timeout=5) == "pong:a"
            order.append("call answered")

    drive_mcp_peer(peer, body)

    assert order == ["released", "listing answered", "call answered"]
    assert len(peer.of("tools/list")) == 2
    assert [sent.method for sent in _credentialed(peer)] == ["tools/call"]


def test_a_call_waiting_on_a_listing_that_fails_gives_up_with_that_session(monkeypatch) -> None:
    monkeypatch.setattr(connection_mod, "_RECONNECT_MIN_DELAY_SEC", 5.0)
    monkeypatch.setattr(connection_mod, "_RECONNECT_WAIT_SEC", 30.0)
    peer = McpPeer()
    listing_started = asyncio.Event()
    fail_listing = asyncio.Event()

    async def body() -> None:
        async with _connected() as connection:

            async def failing_second_listing() -> httpx2.Response:
                listing_started.set()
                await fail_listing.wait()
                return httpx2.Response(500, text="listing broke")

            peer.on_list = failing_second_listing
            connection._drop_session()
            await asyncio.wait_for(listing_started.wait(), timeout=10)
            loop = asyncio.get_running_loop()
            started = loop.time()
            call = asyncio.create_task(_call(connection, ALICE, timeout=30))
            await asyncio.sleep(0.2)
            fail_listing.set()
            result = await asyncio.wait_for(call, timeout=5)
            assert "is not connected" in result
            assert loop.time() - started < 2

    drive_mcp_peer(peer, body)

    assert peer.of("tools/call") == []


def test_a_tool_absent_from_the_listing_is_refused_without_a_request() -> None:
    peer = McpPeer()

    async def body() -> None:
        async with _connected() as connection:
            result = await _call(connection, ALICE, tool="not-listed")
            assert result.startswith("⚠️ MCP_TOOL_ERROR (not-listed)")
            assert "did not list this tool" in result

    drive_mcp_peer(peer, body)

    assert peer.of("tools/call") == []
    assert _credentialed(peer) == []


def test_a_cookie_set_on_one_owners_call_reaches_no_later_request(monkeypatch) -> None:
    monkeypatch.setattr(connection_mod, "_HEALTHCHECK_INTERVAL_SEC", 0.05)
    peer = McpPeer()

    async def set_a_session_cookie(sent: McpPeerRequest) -> httpx2.Response | None:
        if sent.headers.get(TOKEN_HEADER) == "alice-token":
            return peer.pong(sent, **{"set-cookie": "session=s1; Path=/"})
        return None

    peer.on_call = set_a_session_cookie

    async def body() -> None:
        async with _connected() as connection:
            assert await _call(connection, ALICE, who="a") == "pong:a"
            assert [len(client.cookies.jar) for client in peer.clients] == [0]
            assert await _call(connection, BOB, who="b") == "pong:b"
            peer.pinged.clear()
            await asyncio.wait_for(peer.pinged.wait(), timeout=5)

    drive_mcp_peer(peer, body)

    alice, bob = peer.of("tools/call")
    assert alice.headers["cookie"] == "sudir=alice-cookie"
    assert bob.headers["cookie"] == "sudir=bob-cookie"
    for sent in peer.sent:
        assert "session=s1" not in sent.headers.get("cookie", "")
    assert all("cookie" not in sent.headers for sent in peer.sent if sent.method != "tools/call")


def test_concurrent_calls_of_two_owners_each_carry_their_own_credentials() -> None:
    peer = McpPeer()
    both_arrived = asyncio.Event()

    async def hold_until_both_arrive(sent: McpPeerRequest) -> None:
        if len(peer.of("tools/call")) == 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), timeout=5)
        return None

    peer.on_call = hold_until_both_arrive

    async def body() -> None:
        async with _connected() as connection:
            results = await asyncio.gather(
                _call(connection, ALICE, who="alice"),
                _call(connection, BOB, who="bob"),
            )
            assert results == ["pong:alice", "pong:bob"]

    drive_mcp_peer(peer, body)

    by_caller = {sent.arguments["who"]: sent.headers for sent in peer.of("tools/call")}
    assert by_caller["alice"][TOKEN_HEADER] == "alice-token"
    assert by_caller["alice"]["cookie"] == "sudir=alice-cookie"
    assert by_caller["bob"][TOKEN_HEADER] == "bob-token"
    assert by_caller["bob"]["cookie"] == "sudir=bob-cookie"


def test_a_rejected_credential_fails_one_call_and_keeps_the_shared_session() -> None:
    peer = McpPeer()
    bob_arrived = asyncio.Event()

    async def reject_alice(sent: McpPeerRequest) -> httpx2.Response | None:
        if sent.headers.get(TOKEN_HEADER) == "bob-token":
            bob_arrived.set()
            await asyncio.sleep(0.1)
            return None
        await asyncio.wait_for(bob_arrived.wait(), timeout=5)
        return httpx2.Response(401, text="unauthorized")

    peer.on_call = reject_alice

    async def body() -> None:
        async with _connected() as connection:
            session = connection._session
            alice, bob = await asyncio.gather(
                _call(connection, ALICE, who="alice"),
                _call(connection, BOB, who="bob"),
            )
            assert alice.startswith("⚠️ MCP_TOOL_ERROR (echo)")
            assert bob == "pong:bob"
            await asyncio.sleep(0.2)
            assert connection._session is session
            assert await _call(connection, BOB, who="again") == "pong:again"

    drive_mcp_peer(peer, body)

    assert len(peer.of("server/discover")) == 1
    assert len(peer.of("initialize")) == 1


class _ResumptionRefusingPeer(McpPeer):
    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET" and "last-event-id" in request.headers:
            self.sent.append(McpPeerRequest("resume", str(request.url), httpx2.Headers(request.headers), {}))
            return httpx2.Response(401, text="unauthorized")
        return await super().handle(request)


def test_a_call_whose_resumption_is_refused_fails_alone_and_keeps_the_shared_session() -> None:
    peer = _ResumptionRefusingPeer()
    alice_answered = asyncio.Event()

    async def end_alices_stream_early(sent: McpPeerRequest) -> httpx2.Response | None:
        if sent.headers.get(TOKEN_HEADER) == "bob-token":
            await asyncio.wait_for(alice_answered.wait(), timeout=10)
            return None
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream", "mcp-session-id": "sess-1"},
            content=b"id: e1\nretry: 10\nevent: message\ndata: \n\n",
        )

    peer.on_call = end_alices_stream_early

    async def body() -> None:
        async with _connected() as connection:
            session = connection._session

            async def alice_call() -> str:
                try:
                    return await _call(connection, ALICE, who="alice", timeout=20)
                finally:
                    alice_answered.set()

            alice, bob = await asyncio.gather(alice_call(), _call(connection, BOB, who="bob", timeout=20))
            assert alice.startswith("⚠️ MCP_TOOL_ERROR (echo)")
            assert bob == "pong:bob"
            assert connection._session is session

    drive_mcp_peer(peer, body)

    assert len(peer.of("initialize")) == 1
    resumptions = peer.of("resume")
    assert resumptions
    assert all(TOKEN_HEADER not in sent.headers and "cookie" not in sent.headers for sent in resumptions)


def test_a_cross_origin_redirect_drops_the_credentials_and_a_same_origin_one_keeps_the_header() -> None:
    peer = McpPeer()

    async def redirect(sent: McpPeerRequest) -> httpx2.Response | None:
        target = {"cross": "https://elsewhere.test/mcp", "same": "https://mcp.test/mcp/hop"}.get(sent.arguments["who"])
        if target is None or sent.url != ENTRY.url:
            return None
        return httpx2.Response(307, headers={"location": target})

    peer.on_call = redirect

    async def body() -> None:
        async with _connected() as connection:
            assert await _call(connection, ALICE, who="cross") == "pong:cross"
            assert await _call(connection, ALICE, who="same") == "pong:same"

    drive_mcp_peer(peer, body)

    hops = {(sent.arguments["who"], sent.url): sent.headers for sent in peer.of("tools/call")}
    assert hops[("cross", ENTRY.url)][TOKEN_HEADER] == "alice-token"
    elsewhere = hops[("cross", "https://elsewhere.test/mcp")]
    assert TOKEN_HEADER not in elsewhere
    assert "cookie" not in elsewhere
    same = hops[("same", "https://mcp.test/mcp/hop")]
    assert same[TOKEN_HEADER] == "alice-token"
    assert "cookie" not in same


def test_a_static_user_token_over_sse_never_reaches_another_origin() -> None:
    entry = McpServerEntry(
        id="static",
        transport="sse",
        url="https://mcp.test/sse",
        auth={"kind": "user_token", "auth_header": "X-API-Key", "auth_token": "static-secret"},
    )
    seen: list[tuple[str, str | None]] = []
    opened: dict[str, object] = {}

    async def redirect_off_origin(request: httpx2.Request) -> httpx2.Response:
        seen.append((str(request.url), request.headers.get("X-API-Key")))
        if request.url.host == "mcp.test":
            return httpx2.Response(307, headers={"location": "https://elsewhere.test/sse"})
        return httpx2.Response(200)

    @asynccontextmanager
    async def capture_sse_client(url, headers=None, timeout=None, sse_read_timeout=None, httpx_client_factory=None):
        opened.update(headers=headers, factory=httpx_client_factory)
        yield (None, None)

    async def body() -> None:
        with patch.object(transports_mod, "sse_client", capture_sse_client):
            async with open_transport(entry, frozenset()):
                pass
        real_client = httpx2.AsyncClient
        with patch.object(
            httpx2,
            "AsyncClient",
            lambda **kwargs: real_client(transport=httpx2.MockTransport(redirect_off_origin), **kwargs),
        ):
            async with opened["factory"](headers=opened["headers"]) as client:
                await client.get(entry.url)

    asyncio.run(body())

    assert seen == [("https://mcp.test/sse", "static-secret"), ("https://elsewhere.test/sse", None)]


def test_a_refused_authorization_sends_no_call_and_returns_a_tool_error() -> None:
    peer = McpPeer()
    a2a_run = OwnerScope(owner_id="alice", agent_id="main", surface_id=SurfaceId.A2A, chat_id="-1")
    no_context = OwnerScope(owner_id="carol", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="1")
    broken = FixedSecurityContextStore(error=RuntimeError("store down"))
    unsendable = FixedSecurityContextStore(
        {ALICE.security_token: context_of(headers={"x-sudir-token": "tok\r\nX-Injected: yes"})}
    )

    async def body() -> None:
        async with _connected() as connection:
            refused = await _call(connection, no_context)
            assert refused.startswith("⚠️ MCP_TOOL_ERROR (echo)")
            assert "'sudir'" in refused
            assert f"the user credentials lack the header {TOKEN_HEADER!r}" in refused
            assert "surface a2a" in await _call(connection, a2a_run)
        async with _connected(broken) as connection:
            assert "unavailable" in await _call(connection, ALICE)
        async with _connected(unsendable) as connection:
            refused = await _call(connection, ALICE)
            assert "cannot hold" in refused
            assert "Injected" not in refused

    drive_mcp_peer(peer, body)

    assert peer.of("tools/call") == []


def test_a_disconnected_server_reports_not_connected_after_the_reconnect_wait(monkeypatch) -> None:
    monkeypatch.setattr(connection_mod, "_RECONNECT_WAIT_SEC", 0.05)
    store = FixedSecurityContextStore(error=AssertionError("the store is read only for a connected server"))

    async def body() -> None:
        connection = MCPServerConnection(ENTRY, _dependencies(store))
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await connection.call_tool(ALICE, "echo", {}, 30)
        assert "is not connected" in result
        assert loop.time() - started < 1

    asyncio.run(body())

    assert store.loads == []


def test_a_subtask_copying_its_parents_scope_carries_the_parents_credentials(background_loop, tmp_path) -> None:
    peer = McpPeer()
    store = _store()
    manager = MCPManager(background_loop)
    provider = McpToolProvider(
        servers=[ENTRY],
        server_ids_reader=StubMcpServerIdsReader([ENTRY.id]),
        auth_dependencies=_dependencies(store),
        manager=manager,
        timeout_reader=lambda: 5,
        loop=background_loop,
    )
    parent = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path, scope=ALICE)
    requested = SubtaskRequested(scope=parent.scope, task_id="child", description="look it up")
    delivered = decode_agent_event(encode_agent_event(requested))
    child_scope = _task_scope({"id": "child", "scope": delivered.scope.to_dict()})
    subtask = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path, scope=child_scope)

    with patch.object(httpx2, "AsyncClient", mock_mcp_client(peer)):
        provider.refresh()
        try:
            assert provider.execute(subtask, "mcp__sudir__echo", {"who": "subtask"}) == "pong:subtask"
        finally:
            call_on_loop_sync(background_loop, manager.shutdown, timeout_sec=10)

    (call,) = peer.of("tools/call")
    assert call.headers[TOKEN_HEADER] == "alice-token"
    assert store.loads == [
        (
            ALICE.security_token,
            frozenset(
                {
                    SecurityParameterKey(SecurityParameterType.HEADER, TOKEN_HEADER),
                    SecurityParameterKey(SecurityParameterType.COOKIE, "sudir"),
                }
            ),
        )
    ]
