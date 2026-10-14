from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx2
import pytest

import ouroboros.mcp.connection as connection_mod
from infra.user_tokens.interface import UserTokenTarget, UserTokenTargetKind
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.egress.auth.interfaces import EgressAuthDependencies
from ouroboros.mcp.connection import MCPServerConnection
from ouroboros.storage.settings.schema import McpServerEntry
from tests.helpers.mcp_auth import McpPeer, McpPeerRequest, drive_mcp_peer
from tests.helpers.security_context import FixedSecurityContextStore
from tests.helpers.user_tokens import FixedUserTokenStore

SERVER = "hr"
MCP_TARGET = UserTokenTarget(UserTokenTargetKind.MCP_SERVER, SERVER)
A2A_TARGET = UserTokenTarget(UserTokenTargetKind.A2A_AGENT, SERVER)
AUTHORIZATION = "Authorization"
STATIC = "Bearer static-secret-42"
ALICE_TOKEN = "Bearer alice-personal-17"
ALICE = OwnerScope(owner_id="alice", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="1")
BOB = OwnerScope(owner_id="bob", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="1")
CAROL = OwnerScope(owner_id="carol", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="1")
A2A_RUN = OwnerScope(owner_id="alice", agent_id="main", surface_id=SurfaceId.A2A, chat_id="-1")


def _entry(*, header: str = AUTHORIZATION, static: str | None = STATIC) -> McpServerEntry:
    return McpServerEntry(
        id=SERVER,
        url="https://mcp.test/mcp",
        auth={"kind": "user_token", "auth_header": header, "auth_token": static},
    )


def _tokens(**by_owner: str) -> FixedUserTokenStore:
    return FixedUserTokenStore({(MCP_TARGET, owner, "main"): token for owner, token in by_owner.items()})


@asynccontextmanager
async def _connected(tokens: FixedUserTokenStore, entry: McpServerEntry):
    dependencies = EgressAuthDependencies(
        security_context_store=FixedSecurityContextStore(
            error=AssertionError("a user token server read the security context store")
        ),
        user_token_store=tokens,
    )
    connection = MCPServerConnection(entry, dependencies)
    await connection.connect()
    try:
        yield connection
    finally:
        await connection.aclose()


async def _call(connection: MCPServerConnection, scope: OwnerScope, *, who: str = "") -> str:
    return await connection.call_tool(scope, "echo", {"who": who}, 5)


def _values(sent: McpPeerRequest, name: str = AUTHORIZATION) -> list[str]:
    return sent.headers.get_list(name)


@pytest.mark.parametrize("era", ["handshake", "modern"])
def test_discovery_carries_the_static_token_and_a_call_carries_exactly_the_personal_one(era, monkeypatch) -> None:
    monkeypatch.setattr(connection_mod, "_HEALTHCHECK_INTERVAL_SEC", 0.05)
    peer = McpPeer(era=era)

    async def body() -> None:
        async with _connected(_tokens(alice=ALICE_TOKEN), _entry()) as connection:
            assert await _call(connection, ALICE, who="a") == "pong:a"
            if era == "handshake":
                peer.pinged.clear()
                await asyncio.wait_for(peer.pinged.wait(), timeout=5)

    drive_mcp_peer(peer, body)

    (call,) = peer.of("tools/call")
    assert _values(call) == [ALICE_TOKEN]
    others = [sent for sent in peer.sent if sent is not call]
    assert {"server/discover", "tools/list"} <= {sent.method for sent in others}
    assert all(_values(sent) == [STATIC] for sent in others)


def test_without_a_static_token_discovery_carries_no_authorization() -> None:
    peer = McpPeer()

    async def body() -> None:
        async with _connected(_tokens(alice=ALICE_TOKEN), _entry(static=None)) as connection:
            assert await _call(connection, ALICE, who="a") == "pong:a"

    drive_mcp_peer(peer, body)

    (call,) = peer.of("tools/call")
    assert _values(call) == [ALICE_TOKEN]
    assert peer.carrying(AUTHORIZATION) == [call]


def test_concurrent_calls_carry_each_owners_token_or_the_static_one() -> None:
    peer = McpPeer()
    both_arrived = asyncio.Event()

    async def hold_until_both_arrive(sent: McpPeerRequest) -> None:
        if len(peer.of("tools/call")) == 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), timeout=5)
        return None

    peer.on_call = hold_until_both_arrive

    async def body() -> None:
        async with _connected(_tokens(alice=ALICE_TOKEN), _entry()) as connection:
            results = await asyncio.gather(_call(connection, ALICE, who="alice"), _call(connection, BOB, who="bob"))
            assert results == ["pong:alice", "pong:bob"]

    drive_mcp_peer(peer, body)

    by_caller = {sent.arguments["who"]: sent for sent in peer.of("tools/call")}
    assert _values(by_caller["alice"]) == [ALICE_TOKEN]
    assert _values(by_caller["bob"]) == [STATIC]


def test_an_a2a_call_carries_the_static_token_and_reads_no_store() -> None:
    peer = McpPeer()
    tokens = _tokens(alice=ALICE_TOKEN)

    async def body() -> None:
        async with _connected(tokens, _entry()) as connection:
            assert await _call(connection, A2A_RUN, who="a2a") == "pong:a2a"

    drive_mcp_peer(peer, body)

    (call,) = peer.of("tools/call")
    assert _values(call) == [STATIC]
    assert tokens.loads == []


def test_an_a2a_token_with_the_same_id_is_not_sent_to_the_mcp_server() -> None:
    peer = McpPeer()
    tokens = FixedUserTokenStore({(A2A_TARGET, "alice", "main"): ALICE_TOKEN})

    async def body() -> None:
        async with _connected(tokens, _entry()) as connection:
            assert await _call(connection, ALICE, who="alice") == "pong:alice"

    drive_mcp_peer(peer, body)

    (call,) = peer.of("tools/call")
    assert _values(call) == [STATIC]
    assert tokens.loads == [(MCP_TARGET, "alice", "main")]
    assert not any("alice-personal-17" in value for sent in peer.sent for value in sent.headers.values())


def test_a_cookie_the_server_sets_on_a_call_is_not_retained(monkeypatch) -> None:
    monkeypatch.setattr(connection_mod, "_HEALTHCHECK_INTERVAL_SEC", 0.05)
    peer = McpPeer()

    async def set_a_session_cookie(sent: McpPeerRequest) -> httpx2.Response | None:
        if sent.arguments.get("who") == "alice":
            return peer.pong(sent, **{"set-cookie": "session=s1; Path=/"})
        return None

    peer.on_call = set_a_session_cookie

    async def body() -> None:
        async with _connected(_tokens(alice=ALICE_TOKEN), _entry()) as connection:
            assert await _call(connection, ALICE, who="alice") == "pong:alice"
            assert [len(client.cookies.jar) for client in peer.clients] == [0]
            assert await _call(connection, BOB, who="bob") == "pong:bob"
            peer.pinged.clear()
            await asyncio.wait_for(peer.pinged.wait(), timeout=5)

    drive_mcp_peer(peer, body)

    assert peer.carrying("cookie") == []


def test_a_cross_origin_redirect_strips_the_token_and_a_same_origin_one_keeps_it() -> None:
    header = "X-API-Key"
    peer = McpPeer()
    entry = _entry(header=header, static="static-key-42")

    async def redirect(sent: McpPeerRequest) -> httpx2.Response | None:
        target = {"cross": "https://elsewhere.test/mcp", "same": "https://mcp.test/mcp/hop"}.get(sent.arguments["who"])
        if target is None or sent.url != entry.url:
            return None
        return httpx2.Response(307, headers={"location": target})

    peer.on_call = redirect

    async def body() -> None:
        async with _connected(_tokens(alice="alice-key-17"), entry) as connection:
            assert await _call(connection, ALICE, who="cross") == "pong:cross"
            assert await _call(connection, ALICE, who="same") == "pong:same"

    drive_mcp_peer(peer, body)

    hops = {(sent.arguments["who"], sent.url): sent for sent in peer.of("tools/call")}
    assert _values(hops[("cross", entry.url)], header) == ["alice-key-17"]
    assert header not in hops[("cross", "https://elsewhere.test/mcp")].headers
    assert _values(hops[("same", "https://mcp.test/mcp/hop")], header) == ["alice-key-17"]


def test_a_refused_authorization_sends_no_tool_call() -> None:
    peer = McpPeer()
    unsendable = _tokens(alice="evil-value\r\nX-Injected: yes")
    broken = FixedUserTokenStore({}, error=RuntimeError("store down backend-secret-99"))

    async def body() -> None:
        async with _connected(_tokens(alice=ALICE_TOKEN), _entry(static=None)) as connection:
            refused = await _call(connection, CAROL)
            assert refused.startswith("⚠️ MCP_TOOL_ERROR (echo)")
            assert "no user token is stored for this owner and agent" in refused
            assert "surface a2a" in await _call(connection, A2A_RUN)
        async with _connected(broken, _entry()) as connection:
            refused = await _call(connection, ALICE)
            assert "the user token store is unavailable" in refused
            assert "backend-secret-99" not in refused
            assert STATIC not in refused
        async with _connected(unsendable, _entry()) as connection:
            refused = await _call(connection, ALICE)
            assert "cannot hold" in refused
            assert "evil-value" not in refused

    drive_mcp_peer(peer, body)

    assert peer.of("tools/call") == []
