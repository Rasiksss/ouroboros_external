"""Tests for the MCP client — real-SDK connection integration + manager bridging.

Async bodies are driven via asyncio.run(), matching this repo's existing
convention (see tests/ouroboros/observability/test_event_bus.py, tests/conftest.py) rather than
pytest-asyncio, which isn't a project dependency.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import httpx2
import pytest
from mcp import ClientSession, types as mcp_types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp.types import (
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    REQUEST_TIMEOUT,
    ErrorData,
    Implementation,
    InitializeResult,
    JSONRPCError,
    JSONRPCResponse,
    ListToolsResult,
    ServerCapabilities,
    Tool,
)
from mcp_types.version import (
    HANDSHAKE_PROTOCOL_VERSIONS,
    LATEST_HANDSHAKE_VERSION,
    LATEST_MODERN_VERSION,
    MODERN_PROTOCOL_VERSIONS,
)
from pydantic import ValidationError

import ouroboros.mcp.connection as connection_mod
import ouroboros.mcp.transports as transports_mod
from infra.user_tokens.interface import UserTokenTargetKind
from new_core.tools.schema import ToolSchema
from ouroboros.egress.auth.authenticators import authenticator_for, static_headers
from ouroboros.mcp.connection import (
    DiscoveredMCPTool,
    MCPServerConnection,
    _build_mcp_tool_name,
)
from ouroboros.mcp.manager import MCPManager
from ouroboros.mcp.transports import open_transport
from ouroboros.observability import aef_tracing
from ouroboros.storage.settings.schema import McpServerEntry
from ouroboros.tools.naming import sanitize_tool_name
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.egress_auth import null_egress_auth_dependencies, unread_egress_auth_dependencies
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.factories import permissive_safety
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.user_settings import StubMcpServerIdsReader

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


_PROVIDER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _placeholder_entry(server_id, **overrides):
    """An entry for tests that patch `open_transport` and never reach the network.

    The transport is arbitrary for those tests, which is exactly why it lives in one place: the
    next test stands still without having to pick one.
    """
    fields = {"transport": "streamable_http", "url": "https://placeholder.test/mcp", **overrides}
    return McpServerEntry(id=server_id, **fields)


def test_sanitize_tool_name_passes_clean_names_through():
    assert sanitize_tool_name("search") == "search"
    assert sanitize_tool_name("my-tool_2") == "my-tool_2"


def test_sanitize_tool_name_is_provider_safe_and_deterministic():
    for raw in ("a::b", "c/d", "тул", "a.b", "x y", "a?b"):
        sanitized = sanitize_tool_name(raw)
        assert _PROVIDER_NAME_RE.match(sanitized), (raw, sanitized)
        assert sanitized == sanitize_tool_name(raw)


def test_sanitize_tool_name_keeps_lossy_replacements_distinct():
    # All three collapse to "a_b" without disambiguation.
    names = {sanitize_tool_name(raw) for raw in ("a/b", "a.b", "a_b")}
    assert len(names) == 3
    assert sanitize_tool_name("a_b") == "a_b"


def test_build_mcp_tool_name():
    assert _build_mcp_tool_name("my-server", "search") == "mcp__my-server__search"
    full = _build_mcp_tool_name("a::b", "c/d")
    assert full.startswith("mcp__a__b_")
    assert _PROVIDER_NAME_RE.match(full)


def test_build_mcp_tool_name_caps_length_at_64():
    long_a = _build_mcp_tool_name("server", "x" * 200)
    long_b = _build_mcp_tool_name("server", "x" * 200 + "y")
    assert len(long_a) == 64
    assert _PROVIDER_NAME_RE.match(long_a)
    assert long_a != long_b
    assert long_a == _build_mcp_tool_name("server", "x" * 200)


client_header_cases = {
    "a static user token": {
        "entry": _placeholder_entry("s", auth={"kind": "user_token", "auth_header": "X-Key", "auth_token": "secret"}),
        "expected": {"X-Key": "secret"},
    },
    "a static user token over sse": {
        "entry": _placeholder_entry(
            "s", transport="sse", auth={"kind": "user_token", "auth_header": "X-Key", "auth_token": "secret"}
        ),
        "expected": {"X-Key": "secret"},
    },
    "a user token without a static one": {
        "entry": _placeholder_entry("s", auth={"kind": "user_token", "auth_header": "X-Key"}),
        "expected": {},
    },
    "user headers": {
        "entry": _placeholder_entry("s", auth={"kind": "user_headers", "headers": ["X-Key"]}),
        "expected": {},
    },
    "no authorization": {"entry": _placeholder_entry("s"), "expected": {}},
    "mtls": {"entry": _placeholder_entry("s", url="http://gateway.test/mcp", auth={"kind": "mtls"}), "expected": {}},
}


@parametrize_from_dict(client_header_cases)
def test_a_static_user_token_is_a_client_header_and_nothing_else_is(entry, expected):
    assert static_headers(entry.auth) == expected


def test_mcp_server_optional_metadata_defaults_are_empty():
    entry = _placeholder_entry("s")

    assert entry.retrieval_aliases == []
    assert entry.tool_result_limits == {}


@pytest.mark.parametrize(
    "tool_result_limits",
    [
        {"query": 0},
        {"query": 120_001},
        {"query": True},
        {"query": 30_000.0},
        {"query": 30_000.5},
        {"query": ""},
        {"": 30_000},
    ],
)
def test_mcp_server_rejects_invalid_tool_result_limits(tool_result_limits):
    with pytest.raises(ValueError):
        _placeholder_entry("s", tool_result_limits=tool_result_limits)


def test_mcp_server_rejects_stdio_transport():
    """`stdio` is gone from the boundary, not merely unreachable behind it.

    A value the schema accepts and `open_transport` cannot open would surface as a per-server
    connect failure — indistinguishable from a server being down.
    """
    with pytest.raises(ValueError):
        McpServerEntry(id="s", transport="stdio", url="https://e.example/mcp")


def test_mcp_server_requires_the_url_key_at_all():
    """An omitted `url` must fail like an empty one.

    Pydantic skips a field's validators when the key is absent and the default stands, so a
    defaulted `url` was checked only for entries that already carried one — and an entry with no
    `url` key reached `open_transport` exactly as before.
    """
    with pytest.raises(ValueError):
        McpServerEntry.model_validate({"id": "s", "transport": "streamable_http"})


@pytest.mark.parametrize("url", ["", "   ", "mcp.example.test/mcp", "ftp://mcp.example.test/mcp"])
def test_mcp_server_requires_an_http_url(url):
    """Both surviving transports are URL-based, so a URL-less entry is unopenable, not optional.

    It was legitimately empty for `stdio`; accepted now it reaches `open_transport` and dies there
    as a connection failure — the same indistinguishable-from-down failure the stdio value had.
    """
    with pytest.raises(ValueError):
        McpServerEntry(id="s", url=url)


def test_mcp_server_rejects_the_deleted_stdio_fields():
    """`command`/`args`/`env` are gone from the payload, not only from the field list.

    Kept as extras they round-trip back out through `model_dump` — a free-form command line and
    whatever secrets sat in `env`, carried by every serialization of a stale entry.
    """
    with pytest.raises(ValueError):
        McpServerEntry.model_validate(
            {
                "id": "legacy",
                "url": "https://e.example/mcp",
                "command": "/bin/sh",
                "args": ["-c", "serve"],
                "env": {"SECRET": "s3cret"},
            }
        )


def test_open_transport_unsupported():
    async def _run():
        # transport is a validated Literal, so bypass validation to exercise
        # the defensive runtime branch directly.
        entry = McpServerEntry.model_construct(id="s", transport="carrier-pigeon")
        with pytest.raises(ValueError, match="Unsupported MCP transport"):
            async with open_transport(entry, frozenset()):
                pass

    asyncio.run(_run())


def test_streamable_http_transport_sets_explicit_http_timeouts():
    """httpx's 5s default fires under the MCP-level tool timeout and kills the session."""
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def _fake_streamable_http_client(url, *, http_client):
        captured["url"] = url
        captured["client"] = http_client
        yield (None, None, None)

    async def _run():
        entry = McpServerEntry(
            id="s",
            transport="streamable_http",
            url="https://example.test/mcp",
            auth={"kind": "user_token", "auth_header": "X-Key", "auth_token": "secret"},
        )
        with patch.object(transports_mod, "streamable_http_client", _fake_streamable_http_client):
            async with open_transport(entry, frozenset()) as (read, write):
                # Handed through unwrapped: the client's request hook stamps each request
                # from the context it was queued under, so nothing is recorded at this frame.
                assert read is None
                assert write is None

    asyncio.run(_run())

    client = captured["client"]
    assert captured["url"] == "https://example.test/mcp"
    assert client.headers["X-Key"] == "secret"
    assert client.follow_redirects is True
    # Built through the same factory the SSE branch hands the SDK, so this transport cannot lose
    # the hook — or a redirect policy — to a hand-assembled client drifting out of step with it.
    assert aef_tracing.add_outbound_headers_on_request in client.event_hooks["request"]
    assert client.timeout.read >= 300.0  # longer than any tool call this app makes
    assert client.timeout.connect == 10.0
    assert client.timeout.write == 30.0
    assert client.timeout.pool == 30.0


def test_sse_transport_sets_explicit_http_timeouts():
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def _fake_sse_client(url, headers=None, timeout=None, sse_read_timeout=None, httpx_client_factory=None):
        captured.update(
            url=url,
            headers=headers,
            timeout=timeout,
            sse_read_timeout=sse_read_timeout,
            httpx_client_factory=httpx_client_factory,
        )
        yield (None, None)

    async def _run():
        entry = McpServerEntry(id="s", transport="sse", url="https://example.test/sse")
        with patch.object(transports_mod, "sse_client", _fake_sse_client):
            async with open_transport(entry, frozenset()) as (read, write):
                # Handed through unwrapped: the client's request hook stamps each request
                # from the context it was queued under, so nothing is recorded at this frame.
                assert read is None
                assert write is None

    asyncio.run(_run())

    assert captured["timeout"] == 30.0
    assert captured["sse_read_timeout"] >= 300.0
    # The SDK builds this transport's client, so the outbound-header hook rides in on the factory.
    # A factory rather than a prepared client because `sse_client` insists on building its own; the
    # hook holds nothing of this connection and reads the identifier at send time, which is why it
    # is `aef_tracing`'s own and not one this module makes.
    factory = captured["httpx_client_factory"]
    # As `sse_client` calls it: one scalar spread over connect/write/pool, `read` from
    # `sse_read_timeout`. Asserted on the built client rather than on the arguments, because the
    # factory narrows what it is handed and only the client says what came out.
    client = factory(headers={"X-API-Key": "k"}, timeout=httpx2.Timeout(30.0, read=300.0))
    try:
        assert client.headers["x-api-key"] == "k"  # the SDK's factory still supplies auth
        # Identity, not a name: this is the hook `aef_tracing` owns, and a look-alike defined here
        # is exactly what the assertion has to fail on.
        assert aef_tracing.add_outbound_headers_on_request in client.event_hooks["request"]
        # One connect budget for both transports: `sse_client`'s scalar would otherwise open this
        # one on 30s while the streamable transport opens on 10s, and the connect timeout in
        # `connection.py` would mean a different thing per transport.
        assert client.timeout.connect == transports_mod.TCP_CONNECT_TIMEOUT_SEC
        # The caller's read survives, so `sse_read_timeout=` above still decides the stream's life.
        assert client.timeout.read == 300.0
    finally:
        asyncio.run(client.aclose())

    other = factory(timeout=httpx2.Timeout(30.0, read=7.0))
    try:
        assert other.timeout.read == 7.0, "sse_read_timeout= must not be dead at the call site"
    finally:
        asyncio.run(other.aclose())


def _opened_streamable_client(entry: McpServerEntry) -> tuple[str, httpx2.AsyncClient]:
    captured: dict[str, object] = {}
    forwarded_headers = authenticator_for(
        entry, UserTokenTargetKind.MCP_SERVER, unread_egress_auth_dependencies()
    ).forwarded_headers

    @asynccontextmanager
    async def _fake_streamable_http_client(url, *, http_client):
        captured["url"] = url
        captured["client"] = http_client
        yield (None, None, None)

    async def _open() -> None:
        with patch.object(transports_mod, "streamable_http_client", _fake_streamable_http_client):
            async with open_transport(entry, forwarded_headers):
                pass

    asyncio.run(_open())
    return str(captured["url"]), cast(httpx2.AsyncClient, captured["client"])


def test_an_mtls_entry_opens_like_one_without_authorization():
    direct_url, direct = _opened_streamable_client(_placeholder_entry("s", url="http://gateway.test/mcp"))
    gateway_url, gateway = _opened_streamable_client(
        _placeholder_entry("s", url="http://gateway.test/mcp", auth={"kind": "mtls"})
    )

    assert gateway_url == direct_url
    assert dict(gateway.headers) == dict(direct.headers)
    assert gateway.event_hooks["request"] == direct.event_hooks["request"]
    assert gateway.timeout == direct.timeout
    assert gateway.follow_redirects == direct.follow_redirects
    assert type(gateway.cookies.jar._policy) is type(direct.cookies.jar._policy)
    assert gateway.cookies.jar._policy.allowed_domains() is None


def test_a_user_token_entry_opens_a_client_that_keeps_no_cookies_and_strips_off_origin():
    _url, client = _opened_streamable_client(
        _placeholder_entry("s", auth={"kind": "user_token", "auth_header": "X-Key", "auth_token": "secret"})
    )

    assert client.headers["X-Key"] == "secret"
    assert client.cookies.jar._policy.allowed_domains() == ()
    assert len(client.event_hooks["request"]) == 2


def test_a_static_user_token_over_sse_rides_the_client_headers_and_no_call():
    captured: dict[str, object] = {}
    entry = _placeholder_entry(
        "s",
        transport="sse",
        url="https://placeholder.test/sse",
        auth={"kind": "user_token", "auth_header": "X-API-Key", "auth_token": "static-secret"},
    )
    authenticator = authenticator_for(entry, UserTokenTargetKind.MCP_SERVER, unread_egress_auth_dependencies())

    @asynccontextmanager
    async def _fake_sse_client(url, headers=None, timeout=None, sse_read_timeout=None, httpx_client_factory=None):
        captured["headers"] = headers
        yield (None, None)

    async def _run() -> dict[str, str]:
        with patch.object(transports_mod, "sse_client", _fake_sse_client):
            async with open_transport(entry, authenticator.forwarded_headers):
                pass
        return await authenticator.request_headers(TEST_SCOPE)

    assert asyncio.run(_run()) == {}
    assert captured["headers"] == {"X-API-Key": "static-secret"}
    assert authenticator.forwarded_headers == frozenset()
    assert MCPServerConnection(entry, unread_egress_auth_dependencies())._credentialed is False


# ---------------------------------------------------------------------------
# MCPServerConnection — real SDK, in-memory transport (no network/subprocess)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _fake_server(app: MCPServer):
    """Run `app` against an in-memory transport and patch open_transport to reach it."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        server_read, server_write = server_streams
        task = asyncio.create_task(
            app._lowlevel_server.run(server_read, server_write, app._lowlevel_server.create_initialization_options())
        )

        @asynccontextmanager
        async def _fake_open_transport(entry, _forwarded_headers):
            yield client_streams

        with patch.object(connection_mod, "open_transport", _fake_open_transport):
            try:
                yield
            finally:
                task.cancel()


def test_connection_connect_lists_and_calls_tool():
    app = MCPServer("greeter")

    @app.tool()
    def greet(name: str) -> str:
        """Say hello."""
        return f"Hello, {name}!"

    async def _run():
        async with _fake_server(app):
            entry = _placeholder_entry("greeter")
            conn = MCPServerConnection(entry, unread_egress_auth_dependencies())
            await conn.connect()

            assert [t.full_name for t in conn.tools] == ["mcp__greeter__greet"]
            assert conn.tools[0].original_name == "greet"
            assert "[MCP greeter]" in conn.tools[0].schema.description

            result = await conn.call_tool(TEST_SCOPE, "greet", {"name": "World"}, timeout_sec=5)
            assert result == "Hello, World!"
            await conn.aclose()

    asyncio.run(_run())


def test_connect_shuts_down_session_task_when_discovery_fails():
    app = MCPServer("greeter")

    @app.tool()
    def greet(name: str) -> str:
        """Say hello."""
        return f"Hello, {name}!"

    async def _boom(self, session):
        raise RuntimeError("discovery failed")

    async def _run():
        async with _fake_server(app):
            entry = _placeholder_entry("greeter")
            conn = MCPServerConnection(entry, unread_egress_auth_dependencies())
            with patch.object(MCPServerConnection, "_refresh_tools", _boom):
                with pytest.raises(RuntimeError, match="discovery failed"):
                    await conn.connect()

            assert conn._session_task is not None
            assert conn._session_task.done()
            assert conn._session is None

    asyncio.run(_run())


def test_connection_call_tool_error_is_formatted():
    app = MCPServer("boomer")

    @app.tool()
    def boom() -> str:
        raise ToolError("kaboom")

    async def _run():
        async with _fake_server(app):
            entry = _placeholder_entry("boomer")
            conn = MCPServerConnection(entry, unread_egress_auth_dependencies())
            await conn.connect()

            result = await conn.call_tool(TEST_SCOPE, "boom", {}, timeout_sec=5)
            assert result.startswith("⚠️ MCP_TOOL_ERROR (boom):")
            assert "kaboom" in result
            await conn.aclose()

    asyncio.run(_run())


def test_a_tool_that_crashes_still_reaches_the_caller_as_an_error():
    """The other half of the error contract, which `ToolError` above no longer covers.

    2.x withholds an unanticipated exception's text from the client on purpose, so what the
    caller gets is the server's generic message — but it must still arrive as `is_error`, and
    still be formatted, rather than surfacing as a successful call whose text reads like prose.
    """
    app = MCPServer("crasher")

    @app.tool()
    def crash() -> str:
        raise ValueError("kaboom")

    async def _run():
        async with _fake_server(app):
            entry = _placeholder_entry("crasher")
            conn = MCPServerConnection(entry, unread_egress_auth_dependencies())
            await conn.connect()

            result = await conn.call_tool(TEST_SCOPE, "crash", {}, timeout_sec=5)
            assert result.startswith("⚠️ MCP_TOOL_ERROR (crash):")
            assert "kaboom" not in result, "2.x keeps the crash text on the server"
            await conn.aclose()

    asyncio.run(_run())


def test_connection_call_tool_native_timeout():
    app = MCPServer("slowpoke")

    @app.tool()
    async def slow() -> str:
        await asyncio.sleep(2)
        return "done"

    async def _run():
        async with _fake_server(app):
            entry = _placeholder_entry("slowpoke")
            conn = MCPServerConnection(entry, unread_egress_auth_dependencies())
            await conn.connect()

            result = await conn.call_tool(TEST_SCOPE, "slow", {}, timeout_sec=0.2)
            assert result.startswith("⚠️ MCP_TOOL_ERROR (slow):")
            await conn.aclose()

    asyncio.run(_run())


def test_connection_allowed_tools_filters_discovery():
    app = MCPServer("multi")

    @app.tool()
    def a_tool() -> str:
        return "a"

    @app.tool()
    def b_tool() -> str:
        return "b"

    async def _run():
        async with _fake_server(app):
            entry = _placeholder_entry("multi", allowed_tools=["a_tool"])
            conn = MCPServerConnection(entry, unread_egress_auth_dependencies())
            await conn.connect()

            assert [t.original_name for t in conn.tools] == ["a_tool"]
            await conn.aclose()

    asyncio.run(_run())


def test_refresh_tools_follows_pagination():
    entry = _placeholder_entry("paged")
    conn = MCPServerConnection(entry, unread_egress_auth_dependencies())

    page_1 = ListToolsResult(
        tools=[Tool(name="one", description="", inputSchema={"type": "object", "properties": {}})],
        nextCursor="page2",
    )
    page_2 = ListToolsResult(
        tools=[Tool(name="two", description="", inputSchema={"type": "object", "properties": {}})],
        nextCursor=None,
    )
    calls: list[str | None] = []

    class _FakeSession:
        async def list_tools(self, cursor=None, *, params=None):
            resolved_cursor = params.cursor if params is not None else cursor
            calls.append(resolved_cursor)
            return page_1 if resolved_cursor is None else page_2

    async def _run():
        session = _FakeSession()
        conn._session = session
        await conn._refresh_tools(session)

    asyncio.run(_run())

    assert calls == [None, "page2"]
    assert [t.original_name for t in conn.tools] == ["one", "two"]


def test_refresh_tools_survives_the_session_being_retired_mid_listing():
    """A fault between two pages retires `self._session`; the listing must not read it again.

    Both detectors run on the session's own task group, so either can null the handle while a page
    is in flight. Re-reading the attribute per page turns that drop into an `AttributeError`, which
    on a first connect becomes the start-up error `MCPManager.initialize` logs before skipping the
    server for the life of the process — there is no ladder behind `connect()`. Listing the session
    it was handed makes the drop a failed request instead, which ends the cycle the ordinary way.
    """
    conn = MCPServerConnection(_placeholder_entry("paged"), unread_egress_auth_dependencies())
    page_1 = ListToolsResult(
        tools=[Tool(name="one", description="", inputSchema={"type": "object", "properties": {}})],
        nextCursor="page2",
    )

    class _DroppingSession:
        async def list_tools(self, cursor=None, *, params=None):
            resolved_cursor = params.cursor if params is not None else cursor
            if resolved_cursor is None:
                # The fault lands between the pages, exactly as a detector would deliver it.
                conn._drop_session()
                return page_1
            raise MCPError.from_error_data(ErrorData(code=mcp_types.CONNECTION_CLOSED, message="Connection closed"))

    session = _DroppingSession()

    async def _run():
        conn._session = session
        await conn._refresh_tools(session)

    # Not `AttributeError`: that is what reading `self._session` per page raises here.
    with pytest.raises(MCPError):
        asyncio.run(_run())


def test_the_first_tool_listing_is_reported(caplog):
    """The change report needs a previous listing, and a first connect has none.

    A tool the client itself refused — the SDK drops any whose `x-mcp-header` annotations are
    invalid on the modern era — is simply absent, and on a process that has connected once nothing
    diffs it against anything. Naming the catalogue that was published is what leaves the absence
    readable rather than invisible.
    """
    conn = MCPServerConnection(_placeholder_entry("published"), unread_egress_auth_dependencies())
    listing = ListToolsResult(
        tools=[Tool(name="kept", description="", inputSchema={"type": "object", "properties": {}})],
        nextCursor=None,
    )

    class _FakeSession:
        async def list_tools(self, cursor=None, *, params=None):
            return listing

    async def _run():
        session = _FakeSession()
        conn._session = session
        await conn._refresh_tools(session)

    with caplog.at_level(logging.INFO, logger="ouroboros.mcp.connection"):
        asyncio.run(_run())

    # Matched on the format string, not the rendered message: the server id is itself "published",
    # so a substring test on the message would pass on a record that never named the catalogue.
    reported = [r.getMessage() for r in caplog.records if r.msg.startswith("MCP server %s listed")]
    assert reported, "the first listing left no record of what the server published"
    assert "mcp__published__kept" in reported[0]


# ---------------------------------------------------------------------------
# MCPServerConnection — surviving a dropped session
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _reconnectable_fake_server(app: MCPServer):
    """Like `_fake_server`, but each open_transport() gets a fresh in-memory server, and the
    server side of each is handed to the test so it can be killed. Yields them, newest last."""
    sessions: list[tuple] = []

    @asynccontextmanager
    async def _fake_open_transport(entry, _forwarded_headers):
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            task = asyncio.create_task(
                app._lowlevel_server.run(
                    server_streams[0], server_streams[1], app._lowlevel_server.create_initialization_options()
                )
            )
            sessions.append((server_streams, task))
            try:
                yield client_streams
            finally:
                task.cancel()

    with patch.object(connection_mod, "open_transport", _fake_open_transport):
        try:
            yield sessions
        finally:
            for _streams, task in sessions:
                task.cancel()


async def _wait_until(predicate, timeout_sec: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met in time")


def _shrink_timings(monkeypatch, healthcheck: float = 0.05):
    monkeypatch.setattr(connection_mod, "_HEALTHCHECK_INTERVAL_SEC", healthcheck)
    monkeypatch.setattr(connection_mod, "_RECONNECT_MIN_DELAY_SEC", 0.05)
    # The resolver rather than the setting: `load_settings()` is a process-wide singleton, so
    # shrinking it through the environment would leak into every test sharing the process.
    monkeypatch.setattr(connection_mod, "get_mcp_request_timeout_sec", lambda: 0.5)


_FAKE_RESULTS = {
    "initialize": InitializeResult(
        protocolVersion=LATEST_HANDSHAKE_VERSION,
        capabilities=ServerCapabilities(),
        serverInfo=Implementation(name="terminator", version="1.0"),
    ).model_dump(by_alias=True, mode="json", exclude_none=True),
    "tools/list": {"tools": [{"name": "echo", "description": "", "inputSchema": {"type": "object"}}]},
    "tools/call": {"content": [{"type": "text", "text": "pong"}], "isError": False},
    "ping": {},
}


@asynccontextmanager
async def _terminated_session_server():
    """Answers every request the way the transport does on a 404 — a synthesized "Session
    terminated" over streams left wide open. Only the first transport is poisoned, so a test
    can watch the reconnect land on a healthy one. Yields (opens, terminate)."""
    opens: list[int] = []
    terminate = asyncio.Event()

    async def _serve(read_stream, write_stream, dead: asyncio.Event) -> None:
        async for message in read_stream:
            root = getattr(message, "message", None)
            request_id = getattr(root, "id", None)
            if request_id is None:
                continue  # a notification: nothing to answer
            if dead.is_set():
                payload = JSONRPCError(
                    jsonrpc="2.0",
                    id=request_id,
                    error=ErrorData(code=INVALID_REQUEST, message="Session terminated"),
                )
            elif root.method not in _FAKE_RESULTS:
                # A server built before 2026-07-28 has no `server/discover`, and this is what it
                # answers for a method it does not implement — which is what sends the client back
                # to the handshake. Written here rather than assumed: these fakes are handshake-era
                # servers, and the era they get is now their own answer rather than a default.
                payload = JSONRPCError(
                    jsonrpc="2.0",
                    id=request_id,
                    error=ErrorData(code=METHOD_NOT_FOUND, message="Method not found"),
                )
            else:
                payload = JSONRPCResponse(jsonrpc="2.0", id=request_id, result=_FAKE_RESULTS[root.method])
            await write_stream.send(SessionMessage(payload))

    opened: list[asyncio.Task] = []

    @asynccontextmanager
    async def _fake_open_transport(entry, _forwarded_headers):
        index = len(opens)
        opens.append(index)
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            dead = terminate if index == 0 else asyncio.Event()
            task = asyncio.create_task(_serve(server_streams[0], server_streams[1], dead))
            opened.append(task)
            try:
                yield client_streams
            finally:
                task.cancel()

    with patch.object(connection_mod, "open_transport", _fake_open_transport):
        try:
            yield opens, terminate
        finally:
            # A failing assertion never unwinds the transport contexts above, so the run this
            # helper exists to reproduce would end in "Task was destroyed but it is pending" on
            # top of the real failure. Its two neighbours do the same.
            for task in opened:
                task.cancel()


def test_the_idle_ping_notices_a_session_the_client_wrote_off(monkeypatch):
    """The SDK turns a 404 into an error *reply*; reading that as proof of life wedges forever."""
    _shrink_timings(monkeypatch)

    async def _run():
        async with _terminated_session_server() as (opens, terminate):
            conn = MCPServerConnection(_placeholder_entry("gone"), unread_egress_auth_dependencies())
            await conn.connect()
            assert opens == [0]

            terminate.set()

            await _wait_until(lambda: len(opens) == 2 and conn._connected.is_set())
            assert await conn.call_tool(TEST_SCOPE, "echo", {}, timeout_sec=5) == "pong"
            await conn.aclose()

    asyncio.run(_run())


def test_call_tool_notices_a_session_the_client_wrote_off(monkeypatch):
    _shrink_timings(monkeypatch, healthcheck=30.0)  # no ping will fire; only the call can tell

    async def _run():
        async with _terminated_session_server() as (opens, terminate):
            conn = MCPServerConnection(_placeholder_entry("gone"), unread_egress_auth_dependencies())
            await conn.connect()

            terminate.set()
            failed = await conn.call_tool(TEST_SCOPE, "echo", {}, timeout_sec=5)
            assert "Session terminated" in failed
            # The next call must not be handed the same dead session; it would hang there.
            assert conn._session is None
            assert not conn._connected.is_set()

            await _wait_until(lambda: len(opens) == 2 and conn._connected.is_set())
            assert await conn.call_tool(TEST_SCOPE, "echo", {}, timeout_sec=5) == "pong"
            await conn.aclose()

    asyncio.run(_run())


class _ErroringSession:
    """A session whose every request fails with one prepared error."""

    def __init__(self, error: ErrorData) -> None:
        self._error = error

    async def send_ping(self) -> None:
        # 2.x's constructor takes (code, message, data); this is the shape the SDK itself raises.
        raise MCPError.from_error_data(self._error)


def test_an_invalid_request_the_server_meant_does_not_write_off_the_session():
    """2.x spells a dead session `INVALID_REQUEST`, a code it also uses for live ones.

    Matching the code alone would tear down a healthy session the first time a server rejected a
    request — the opposite failure from the one `_is_session_gone` exists to catch.
    """
    conn = MCPServerConnection(_placeholder_entry("picky"), unread_egress_auth_dependencies())
    session = _ErroringSession(ErrorData(code=INVALID_REQUEST, message="Invalid Request"))

    async def _run():
        conn._session = session
        await conn._ping(session)  # returns rather than raising: the session is still alive

    asyncio.run(_run())

    assert conn._session is session


def test_a_server_error_in_the_reserved_range_does_not_write_off_the_session():
    """`CONNECTION_CLOSED` is -32000, inside JSON-RPC's implementation-defined server-error range.

    So the code alone cannot say who authored the error. A server that answers with it would
    otherwise cost a rebuilt transport per call — the same false positive the INVALID_REQUEST arm
    above already guards against, left open for the code the client synthesizes.
    """
    conn = MCPServerConnection(_placeholder_entry("noisy"), unread_egress_auth_dependencies())
    session = _ErroringSession(ErrorData(code=mcp_types.CONNECTION_CLOSED, message="rate limited"))

    async def _run():
        conn._session = session
        await conn._ping(session)  # returns rather than raising: the session is still alive

    asyncio.run(_run())

    assert conn._session is session


def test_the_dispatchers_own_close_still_writes_off_the_session():
    """The narrowing must not cost the detection it narrows: the SDK's own wording still counts.

    Driven against the SDK rather than asserted off this module's constant — a bump that reworded
    its synthesis would otherwise switch `_is_session_gone`'s first arm off with the suite green.
    """

    async def _run():
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            await server_streams[0].aclose()
            await server_streams[1].aclose()
            async with ClientSession(client_streams[0], client_streams[1]) as session:
                with pytest.raises(MCPError) as raised:
                    await session.send_ping()
                return raised.value.error

    error = asyncio.run(_run())

    assert error.code == mcp_types.CONNECTION_CLOSED
    assert connection_mod._is_session_gone(error), f"the SDK now spells its own close {error.message!r}"


def test_a_session_the_server_forgot_still_writes_off_the_session():
    """The other arm of `_is_session_gone`, driven against the SDK rather than against itself.

    A streamable-HTTP server that has forgotten a session id answers 404, and the transport turns
    that into an `INVALID_REQUEST` whose *message* is the only thing separating it from a request
    a server merely rejected. `_terminated_session_server` builds that reply by hand, so it
    compares the constant with itself: a bump that reworded the synthesis would leave
    `_note_dead_session` silent, and every later call would go into a session that is gone — with
    this suite green. The 404 has to arrive *after* a session id exists, because without one the
    SDK spells the same status `METHOD_NOT_FOUND` instead.
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST":
            return httpx2.Response(405)  # the standalone GET stream and the goodbye DELETE
        body = json.loads(request.content or b"{}")
        if "id" not in body:
            return httpx2.Response(202)  # a notification
        if body["method"] == "initialize":
            return httpx2.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": _FAKE_RESULTS["initialize"]},
                headers={"mcp-session-id": "sess-1"},
            )
        return httpx2.Response(404)  # the server no longer knows this session

    async def _run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            async with streamable_http_client("http://mcp.test/mcp", http_client=client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    with pytest.raises(MCPError) as raised:
                        await session.send_ping()
                    return raised.value.error

    error = asyncio.run(_run())

    assert error.code == mcp_types.INVALID_REQUEST
    assert connection_mod._is_session_gone(error), f"the SDK now spells a forgotten session {error.message!r}"


def test_the_idle_ping_raises_on_the_dispatchers_own_timeout():
    """`REQUEST_TIMEOUT` is a JSON-RPC code in 2.x, where 1.x reported HTTP 408.

    Read as an ordinary refusal, a server that stopped answering would be logged as alive and the
    connection would never reconnect.
    """
    conn = MCPServerConnection(_placeholder_entry("mute"), unread_egress_auth_dependencies())
    session = _ErroringSession(ErrorData(code=REQUEST_TIMEOUT, message="Request 'ping' timed out"))

    async def _run():
        with pytest.raises(MCPError):
            await conn._ping(session)

    asyncio.run(_run())


def test_connection_reconnects_after_a_silent_drop(monkeypatch):
    """The drop that raises nothing: the server side goes away, and a session has to replace it.

    Driven against a handshake-era server, because that is the era the keepalive belongs to — on
    2026-07-28 nothing polls an idle session, there being no ping to send.

    **This no longer pins the keepalive, and did once.** The trigger below closes the server's write
    end, so the client's read stream ends and `_ReadStreamEndWatcher` retires the session before an
    idle ping is ever due; the test passes with `_ping` stubbed out to a no-op. Before that detector
    existed nothing else ended the cycle, so the same stubbing would have failed it. What covers the
    keepalive end to end is `test_the_idle_ping_reconnects_a_peer_that_went_quiet`, whose peer stops
    answering without closing anything — the one trigger neither fault detector can see.
    """
    _shrink_timings(monkeypatch)
    sessions: list[tuple] = []

    async def _run():
        async with _handshake_only_server(sessions):
            conn = MCPServerConnection(_placeholder_entry("flaky"), unread_egress_auth_dependencies())
            await conn.connect()
            assert len(sessions) == 1
            assert await conn.call_tool(TEST_SCOPE, "echo", {}, timeout_sec=5) == "pong"

            (_server_read, server_write), server_task = sessions[0]
            server_task.cancel()
            await server_write.aclose()

            await _wait_until(lambda: len(sessions) == 2 and conn._connected.is_set())
            assert await conn.call_tool(TEST_SCOPE, "echo", {}, timeout_sec=5) == "pong"
            assert conn._inflight == 0
            await conn.aclose()

    asyncio.run(_run())


@asynccontextmanager
async def _muting_handshake_server():
    """A handshake-era server that stops answering while its streams stay open.

    The one trigger neither fault detector can see: nothing is closed, so `_ReadStreamEndWatcher` has
    no end to report, and nothing is put on the read stream, so `_on_incoming_message` has no item.
    Only the idle keepalive can notice, which is what makes this the keepalive's own integration
    test. Only the first transport goes quiet, so the reconnect lands on a server that answers.
    Yields (opens, mute).
    """
    opens: list[int] = []
    mute = asyncio.Event()

    async def _serve(read_stream, write_stream, quiet: asyncio.Event) -> None:
        async for message in read_stream:
            root = getattr(message, "message", None)
            request_id = getattr(root, "id", None)
            if request_id is None:
                continue  # a notification: nothing to answer
            if quiet.is_set():
                continue  # read on and answer nothing: the peer is there, and mute
            if root.method in _FAKE_RESULTS:
                payload = JSONRPCResponse(jsonrpc="2.0", id=request_id, result=_FAKE_RESULTS[root.method])
            else:
                payload = JSONRPCError(
                    jsonrpc="2.0", id=request_id, error=ErrorData(code=METHOD_NOT_FOUND, message="Method not found")
                )
            await write_stream.send(SessionMessage(payload))

    # Tasks, not the `(streams, task)` pairs the sibling helpers below collect: nothing here needs
    # a handle on the server side, and an annotation copied from them invites the cleanup to be
    # copied too — which would raise on the unpack, inside a `finally`, hiding the real failure.
    opened: list[asyncio.Task] = []

    @asynccontextmanager
    async def _fake_open_transport(entry, _forwarded_headers):
        index = len(opens)
        opens.append(index)
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            quiet = mute if index == 0 else asyncio.Event()
            task = asyncio.create_task(_serve(server_streams[0], server_streams[1], quiet))
            opened.append(task)
            try:
                yield client_streams
            finally:
                task.cancel()

    with patch.object(connection_mod, "open_transport", _fake_open_transport):
        try:
            yield opens, mute
        finally:
            # A failing assertion never unwinds the transport contexts above, and the run this
            # helper exists to reproduce would then end in "Task was destroyed but it is pending".
            for task in opened:
                task.cancel()


def test_the_idle_ping_reconnects_a_peer_that_went_quiet(monkeypatch):
    """ping → drop → reconnect, end to end, on the era the keepalive belongs to.

    The peer stops answering and closes nothing, so both fault detectors stay silent and the
    keepalive is the only thing left that can notice. What this pins is the whole path — an
    unanswered ping raising out of `_hold_session`, `_supervise` catching it and climbing its
    ladder, and a session that serves calls again — which the unit tests around `_ping` do not
    reach: they assert what `_ping` returns and raises, and nothing about what that causes.
    """
    _shrink_timings(monkeypatch)
    monkeypatch.setattr(connection_mod, "_PING_TIMEOUT_SEC", 0.2)

    async def _run():
        async with _muting_handshake_server() as (opens, mute):
            conn = MCPServerConnection(_placeholder_entry("quiet"), unread_egress_auth_dependencies())
            await conn.connect()
            assert opens == [0]
            assert conn._session is not None
            assert conn._session.discover_result is None, "the keepalive belongs to the handshake era"
            assert await conn.call_tool(TEST_SCOPE, "echo", {}, timeout_sec=5) == "pong"

            mute.set()

            await _wait_until(lambda: len(opens) == 2 and conn._connected.is_set())
            assert await conn.call_tool(TEST_SCOPE, "echo", {}, timeout_sec=5) == "pong"
            assert conn._inflight == 0
            await conn.aclose()

    asyncio.run(_run())


def test_a_quiet_peer_is_invisible_to_both_fault_detectors(monkeypatch):
    """The trigger above is the keepalive's alone, and this is what says so.

    Without it the test above would keep passing on a revision where one of the detectors happened
    to fire instead — which is exactly how the keepalive lost its integration coverage the last
    time. With `_ping` stubbed out, nothing may reconnect.
    """
    _shrink_timings(monkeypatch)

    async def _no_ping(self, session):
        return None

    async def _run():
        async with _muting_handshake_server() as (opens, mute):
            with patch.object(MCPServerConnection, "_ping", _no_ping):
                conn = MCPServerConnection(_placeholder_entry("quiet"), unread_egress_auth_dependencies())
                await conn.connect()
                mute.set()

                # Long enough for many ping intervals and many rungs of the 0.05s ladder.
                await asyncio.sleep(20 * connection_mod._HEALTHCHECK_INTERVAL_SEC)

                assert opens == [0], "something other than the keepalive noticed the quiet peer"
                await conn.aclose()

    asyncio.run(_run())


def test_call_tool_reconnects_when_the_session_streams_are_gone(monkeypatch):
    app = MCPServer("flaky")

    @app.tool()
    def echo() -> str:
        return "pong"

    _shrink_timings(monkeypatch)

    async def _run():
        async with _reconnectable_fake_server(app) as sessions:
            conn = MCPServerConnection(_placeholder_entry("flaky"), unread_egress_auth_dependencies())
            await conn.connect()

            (server_read, _server_write), server_task = sessions[0]
            server_task.cancel()
            await server_read.aclose()  # the client's next send has nowhere to go

            failed = await conn.call_tool(TEST_SCOPE, "echo", {}, timeout_sec=5)
            assert failed.startswith("⚠️ MCP_TOOL_ERROR (echo):")
            assert conn._session is None

            await _wait_until(lambda: len(sessions) == 2 and conn._connected.is_set())
            assert await conn.call_tool(TEST_SCOPE, "echo", {}, timeout_sec=5) == "pong"
            assert conn._inflight == 0
            await conn.aclose()

    asyncio.run(_run())


def test_first_connect_failure_is_reported_and_not_retried(monkeypatch):
    """A server that never came up is the manager's to drop, not this class's to retry."""
    monkeypatch.setattr(connection_mod, "_RECONNECT_MIN_DELAY_SEC", 0.01)
    attempts: list[str] = []

    @asynccontextmanager
    async def _always_fails(entry, _forwarded_headers):
        attempts.append(entry.id)
        raise RuntimeError("no route to host")
        yield  # pragma: no cover

    async def _run():
        with patch.object(connection_mod, "open_transport", _always_fails):
            conn = MCPServerConnection(_placeholder_entry("down"), unread_egress_auth_dependencies())
            with pytest.raises(RuntimeError, match="no route to host"):
                await conn.connect()
            await asyncio.sleep(0.1)

        assert attempts == ["down"]
        assert conn._session_task.done()
        assert conn._session is None

    asyncio.run(_run())


def test_aclose_stops_a_reconnect_in_progress(monkeypatch):
    """Shutdown has to reach the supervisor mid-backoff, not just mid-session."""
    monkeypatch.setattr(connection_mod, "_RECONNECT_MIN_DELAY_SEC", 30.0)
    opens: list[str] = []

    @asynccontextmanager
    async def _open_once_then_fail(entry, _forwarded_headers):
        opens.append(entry.id)
        if len(opens) > 1:
            raise RuntimeError("still down")
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            app = MCPServer("brief")
            task = asyncio.create_task(
                app._lowlevel_server.run(
                    server_streams[0], server_streams[1], app._lowlevel_server.create_initialization_options()
                )
            )
            try:
                yield client_streams
            finally:
                task.cancel()

    async def _run():
        with patch.object(connection_mod, "open_transport", _open_once_then_fail):
            conn = MCPServerConnection(_placeholder_entry("brief"), unread_egress_auth_dependencies())
            await conn.connect()
            conn._stop_serving.set()  # drop it; the supervisor lands in a 30s backoff
            await _wait_until(lambda: not conn._connected.is_set())

            await asyncio.wait_for(conn.aclose(), timeout=5)
            assert conn._session_task.done()
            assert opens == ["brief"]  # aclose() reached it in the backoff, before it re-opened

    asyncio.run(_run())


def test_aclose_gives_up_on_a_teardown_that_hangs(monkeypatch):
    """A gateway that swallows the goodbye DELETE would otherwise hold shutdown for read=300s."""
    monkeypatch.setattr(connection_mod, "_CLOSE_TIMEOUT_SEC", 0.2)

    @asynccontextmanager
    async def _hangs_on_exit(entry, _forwarded_headers):
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            app = MCPServer("stuck")
            task = asyncio.create_task(
                app._lowlevel_server.run(
                    server_streams[0], server_streams[1], app._lowlevel_server.create_initialization_options()
                )
            )
            try:
                yield client_streams
            finally:
                task.cancel()
                await asyncio.sleep(30)  # the goodbye that never comes back

    async def _run():
        with patch.object(connection_mod, "open_transport", _hangs_on_exit):
            conn = MCPServerConnection(_placeholder_entry("stuck"), unread_egress_auth_dependencies())
            await conn.connect()

            loop = asyncio.get_running_loop()
            started = loop.time()
            await asyncio.wait_for(conn.aclose(), timeout=5)
            waited = loop.time() - started

            assert waited < 2.0, f"aclose() sat on a hung teardown for {waited:.1f}s"
            assert conn._session_task.done()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# The protocol era — negotiated per connection, with the handshake as the floor
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _handshake_only_server(sessions: list | None = None):
    """A server built before the revision: no `server/discover`, and the handshake it replaced.

    Hand-rolled rather than an `MCPServer` with the handler removed. This SDK's server decides the
    era of a *stream* from the first envelope it sees, so a probe it refuses still locks the stream
    modern and the client's fallback `initialize` is then rejected — an artifact of sharing one
    stream, which the HTTP transports do not do. A pre-revision peer is exactly a JSON-RPC server
    without that method, so it is modelled as one. Yields the methods it was asked for, in order.
    """
    asked: list[str] = []

    async def _serve(read_stream, write_stream) -> None:
        async for message in read_stream:
            root = getattr(message, "message", None)
            request_id = getattr(root, "id", None)
            if root is not None:
                asked.append(root.method)
            if request_id is None:
                continue  # a notification: nothing to answer
            if root.method in _FAKE_RESULTS:
                payload = JSONRPCResponse(jsonrpc="2.0", id=request_id, result=_FAKE_RESULTS[root.method])
            else:
                payload = JSONRPCError(
                    jsonrpc="2.0",
                    id=request_id,
                    error=ErrorData(code=METHOD_NOT_FOUND, message="Method not found"),
                )
            await write_stream.send(SessionMessage(payload))

    opened: list[tuple] = []

    @asynccontextmanager
    async def _fake_open_transport(entry, _forwarded_headers):
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            task = asyncio.create_task(_serve(server_streams[0], server_streams[1]))
            # Recorded unconditionally; `sessions` is only the caller's view of the same thing, so
            # a caller that wants no view still gets its tasks cancelled below.
            opened.append((server_streams, task))
            if sessions is not None:
                sessions.append((server_streams, task))
            try:
                yield client_streams
            finally:
                task.cancel()

    with patch.object(connection_mod, "open_transport", _fake_open_transport):
        try:
            yield asked
        finally:
            # Each transport cancels its own task, but only once its context unwinds — which for a
            # failing assertion never happens. Without this the run that this helper exists to
            # reproduce ends in "Task was destroyed but it is pending" on top of the real failure.
            for _streams, task in opened:
                task.cancel()


def test_a_connection_adopts_the_era_the_server_speaks():
    """The revision is asked for, not configured: a server that answers the probe gets adopted."""
    app = MCPServer("modern")

    @app.tool()
    def greet() -> str:
        """Say hello."""
        return "hi"

    async def _run():
        async with _fake_server(app):
            conn = MCPServerConnection(_placeholder_entry("modern"), unread_egress_auth_dependencies())
            await conn.connect()
            try:
                assert conn._session is not None
                assert conn._session.protocol_version == LATEST_MODERN_VERSION
                assert conn._session.discover_result is not None
                assert [tool.original_name for tool in conn.tools] == ["greet"]
            finally:
                await conn.aclose()

    asyncio.run(_run())


def test_a_server_without_the_probe_keeps_the_handshake():
    """The binding constraint: a pre-revision server serves exactly what it served before.

    Not "it does not crash" — the same era, the same tools, the same call results. Anything less
    and the negotiation is a change to every server that has not been upgraded yet.
    """

    async def _run():
        async with _handshake_only_server() as asked:
            conn = MCPServerConnection(_placeholder_entry("legacy"), unread_egress_auth_dependencies())
            await conn.connect()
            try:
                assert conn._session is not None
                assert conn._session.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS
                assert conn._session.initialize_result is not None
                assert conn._session.discover_result is None
                assert [tool.original_name for tool in conn.tools] == ["echo"]
                assert await conn.call_tool(TEST_SCOPE, "echo", {}, timeout_sec=5) == "pong"
                # The probe was tried and refused; the handshake is what carried the session.
                assert asked[:2] == ["server/discover", "initialize"]
            finally:
                await conn.aclose()

    asyncio.run(_run())


def test_no_keepalive_is_sent_on_the_newer_era(monkeypatch):
    """`ping` left the protocol in 2026-07-28: sending one asks a question the peer no longer has.

    Asserted on `ClientSession.send_ping` rather than on our own branch, so a gate that stops
    working — or one written against the transport name instead of the era — fails here.
    """
    _shrink_timings(monkeypatch)
    app = MCPServer("modern")

    @app.tool()
    def echo() -> str:
        return "pong"

    pings: list[str] = []

    async def _record_ping(_self) -> None:
        pings.append("ping")

    async def _run():
        async with _fake_server(app):
            with patch.object(ClientSession, "send_ping", _record_ping):
                conn = MCPServerConnection(_placeholder_entry("modern"), unread_egress_auth_dependencies())
                await conn.connect()
                try:
                    assert conn._session is not None
                    assert conn._session.protocol_version == LATEST_MODERN_VERSION
                    await asyncio.sleep(6 * connection_mod._HEALTHCHECK_INTERVAL_SEC)
                    assert pings == []
                finally:
                    await conn.aclose()

    asyncio.run(_run())


def test_the_keepalive_still_runs_on_a_handshake_session(monkeypatch):
    """The era gate must suppress the ping on the newer revision and nowhere else."""
    _shrink_timings(monkeypatch)

    async def _run():
        async with _handshake_only_server() as asked:
            conn = MCPServerConnection(_placeholder_entry("legacy"), unread_egress_auth_dependencies())
            await conn.connect()
            try:
                await _wait_until(lambda: asked.count("ping") >= 2)
            finally:
                await conn.aclose()

    asyncio.run(_run())


def test_the_keepalive_gate_reads_the_era_and_not_the_transport(monkeypatch):
    """`sse` reaches 2026-07-28 too, so the suppression must hold for an `sse` entry as well.

    **What this covers is the branch, not the wire.** `open_transport` is patched away, so the
    entry's transport reaches nothing but `_hold_session`'s decision: it must read the negotiated
    revision, and a gate rewritten against `entry.transport` fails here. Every other test in this
    file carries the streamable entry, so without this one the spec's "on every transport" would
    hold for exactly one transport.

    **What it does not cover** is that a real `sse_client` session survives going idle. That needs a
    live `sse_client` against a modern server with `sse_read_timeout` shortened below the peer's
    keep-alive interval, i.e. an in-process HTTP server this suite has no fixture for. It was
    measured by hand instead — 400s idle against the helper on 8931 dropped nothing, because
    `sse-starlette` writes a `: ping` comment into the stream every 15s, which resets the read
    timeout without surfacing as an event. Shortening the timeout below that interval does drop the
    stream, and `test_a_transport_fault_ends_a_modern_session_and_reconnects` is what pins the
    recovery. Recorded here rather than only in the change's tasks file, which gets archived.
    """
    _shrink_timings(monkeypatch)
    app = MCPServer("modern-sse")

    @app.tool()
    def echo() -> str:
        return "pong"

    pings: list[str] = []

    async def _record_ping(_self) -> None:
        pings.append("ping")

    async def _run():
        async with _fake_server(app):
            with patch.object(ClientSession, "send_ping", _record_ping):
                entry = _placeholder_entry("modern-sse", transport="sse", url="https://placeholder.test/sse")
                conn = MCPServerConnection(entry, unread_egress_auth_dependencies())
                await conn.connect()
                try:
                    assert conn._session is not None
                    assert conn._session.discover_result is not None, "this asserts nothing off the modern era"
                    await asyncio.sleep(6 * connection_mod._HEALTHCHECK_INTERVAL_SEC)
                    assert pings == []
                finally:
                    await conn.aclose()

    asyncio.run(_run())


def test_a_transport_fault_ends_a_modern_session_and_reconnects(monkeypatch):
    """The drop that raises nothing on an era with no ping to notice it.

    An exception on the read stream is how the SDK reports a transport-level fault: the dispatcher
    hands it to `message_handler` and its loop then ends on EOF, so the session's context manager
    returns normally and no caller ever sees it. This is the shape of an `sse` connection reaching
    `sse_read_timeout`, which happens on the modern era exactly as it does on the handshake one —
    its `GET` stream is opened before any negotiation. Without the handler the connection would sit
    on `_stop_serving` forever, advertising a session it no longer has, and the next tool call
    would fail against it before anything reconnected.
    """
    _shrink_timings(monkeypatch)
    app = MCPServer("modern")

    @app.tool()
    def echo() -> str:
        return "pong"

    async def _run():
        async with _reconnectable_fake_server(app) as sessions:
            conn = MCPServerConnection(_placeholder_entry("modern"), unread_egress_auth_dependencies())
            await conn.connect()
            assert conn._session is not None
            assert conn._session.discover_result is not None, "this asserts nothing off the modern era"
            assert len(sessions) == 1

            # The exact class `sse_reader` puts on the stream when nothing arrives in time.
            (_server_read, server_write), _task = sessions[0]
            await server_write.send(httpx2.ReadTimeout("sse_read_timeout"))

            await _wait_until(lambda: len(sessions) == 2 and conn._connected.is_set())
            assert await conn.call_tool(TEST_SCOPE, "echo", {}, timeout_sec=5) == "pong"
            await conn.aclose()

    asyncio.run(_run())


def _ladder_rungs(monkeypatch, drops: int = 3, hold_rungs: float = 0.0) -> list[float]:
    """Each reconnect delay a peer earns by dropping every session after `hold_rungs` of serving,
    as a multiple of `_RECONNECT_MIN_DELAY_SEC`.

    The shape a gateway with a short idle timeout produces on `sse`: the negotiation completes, the
    tools list, and then the stream the transport holds goes away. The two detectors make that a
    noticed drop within milliseconds — which is what puts the ladder's reset rule under load, since
    before them a session that died without raising was only noticed by the next keepalive.

    `hold_rungs` is what separates the two peers the rule has to tell apart, and it is spent as real
    session lifetime rather than by moving a threshold: the rule compares a session's life against
    the pause that would replace it, so a test that patched a constant would no longer be driving
    the rule at all.

    Only the waits on `_close_requested` are the ladder's: `_hold_session` waits on `_stop_serving`
    and `_await_session` on `_connected`, both through the same helper.

    Rungs rather than seconds, in and out: `_shrink_timings` runs inside this helper, so a caller
    reading `_RECONNECT_MIN_DELAY_SEC` to build its expectation reads the unshrunk one and compares
    against a scale that no longer applies.
    """
    _shrink_timings(monkeypatch)
    app = MCPServer("flapping")

    @app.tool()
    def echo() -> str:
        return "pong"

    waits: list[tuple] = []
    real_wait_for = connection_mod._wait_for

    async def _recording_wait_for(event, timeout_sec):
        waits.append((event, timeout_sec))
        return await real_wait_for(event, timeout_sec)

    monkeypatch.setattr(connection_mod, "_wait_for", _recording_wait_for)
    connections: list[MCPServerConnection] = []

    async def _run():
        async with _reconnectable_fake_server(app) as sessions:
            conn = MCPServerConnection(_placeholder_entry("flapping"), unread_egress_auth_dependencies())
            connections.append(conn)
            await conn.connect()
            try:
                hold_sec = hold_rungs * connection_mod._RECONNECT_MIN_DELAY_SEC
                for opened in range(1, drops + 1):
                    await _wait_until(lambda n=opened: len(sessions) == n and conn._connected.is_set())
                    if hold_sec:
                        await asyncio.sleep(hold_sec)
                    (_server_read, server_write), _task = sessions[opened - 1]
                    # The exact class `sse_reader` puts on the stream when nothing arrives in time.
                    await server_write.send(httpx2.ReadTimeout("sse_read_timeout"))
                closing = conn._close_requested
                await _wait_until(lambda: sum(event is closing for event, _t in waits) >= drops)
            finally:
                await conn.aclose()

    asyncio.run(_run())
    closing = connections[0]._close_requested
    minimum = connection_mod._RECONNECT_MIN_DELAY_SEC
    return [timeout / minimum for event, timeout in waits if event is closing][:drops]


def test_a_peer_that_drops_every_fresh_session_climbs_the_reconnect_ladder(monkeypatch):
    """Having served is not having survived, and only the second earns the bottom rung back.

    A reset keyed on "this cycle served" alone is unreachable in practice only while the keepalive
    is the detector — 30s of session life before anything notices. With the two detectors a peer
    that completes the negotiation and closes its held stream is back at the bottom rung within
    milliseconds, so the ladder never climbs and the connection rebuilds the transport,
    re-negotiates the era and re-lists the tools once per `_RECONNECT_MIN_DELAY_SEC`, unbounded,
    for as long as the process lives.
    """
    assert _ladder_rungs(monkeypatch, hold_rungs=0.0) == pytest.approx([1.0, 2.0, 4.0])


def test_a_session_that_outlived_its_own_replacement_cost_keeps_the_bottom_rung(monkeypatch):
    """The other half of the rule: a peer that serves for longer than the pause must not be made
    to wait, however many times it does it.

    This is the case an absolute threshold gets wrong, and it is not exotic — a gateway with an
    idle timeout shorter than the threshold produces it forever. Every session here dies exactly
    the way the climbing peer's does; the only difference is that it served for longer than the
    delay that would replace it, which is the whole of the rule. Under a fixed
    `_SESSION_STABLE_AFTER_SEC` this peer climbed to `_RECONNECT_MAX_DELAY_SEC` and spent most of
    the process disconnected.
    """
    assert _ladder_rungs(monkeypatch, hold_rungs=4.0) == pytest.approx([1.0, 1.0, 1.0])


def test_a_frame_the_client_cannot_parse_does_not_end_the_session(monkeypatch):
    """A malformed frame is the peer's fault, not the transport's, and the session survives it.

    `sse_reader` pushes the parse error onto the read stream and goes on reading, so the exception
    arrives by the same route a real transport fault does. Treating every exception alike would
    answer one bad frame with a reconnect — and a peer emitting them steadily with one reconnect
    per frame, which is worse than the lost message it is reacting to.
    """
    _shrink_timings(monkeypatch)
    app = MCPServer("modern")

    @app.tool()
    def echo() -> str:
        return "pong"

    async def _run():
        async with _reconnectable_fake_server(app) as sessions:
            conn = MCPServerConnection(_placeholder_entry("modern"), unread_egress_auth_dependencies())
            await conn.connect()
            assert len(sessions) == 1
            session = conn._session

            (_server_read, server_write), _task = sessions[0]
            with pytest.raises(ValidationError) as raised:
                mcp_types.jsonrpc_message_adapter.validate_json("{not json", by_name=False)
            await server_write.send(raised.value)

            # Long enough that a reconnect would have landed: the ladder is 0.05s here.
            await asyncio.sleep(0.5)
            assert len(sessions) == 1, "a malformed frame was answered with a reconnect"
            assert conn._connected.is_set()
            assert conn._session is session, "the live session was torn down"
            assert await conn.call_tool(TEST_SCOPE, "echo", {}, timeout_sec=5) == "pong"
            await conn.aclose()

    asyncio.run(_run())


def test_a_frame_the_client_cannot_parse_is_reported_without_its_contents(caplog):
    """A frame is a server's output, and may carry credentials or personal data.

    `str(ValidationError)` renders `input_value=` for every union arm it tried, so rendering the
    error is rendering the frame. The record has to name where the parse failed without carrying
    what failed to parse.
    """
    conn = MCPServerConnection(_placeholder_entry("chatty"), unread_egress_auth_dependencies())
    frame = json.dumps({"jsonrpc": "1.0", "id": 1, "result": {"api_key": "AKIA-SUPER-SECRET"}})
    with pytest.raises(ValidationError) as raised:
        mcp_types.jsonrpc_message_adapter.validate_json(frame, by_name=False)
    assert "AKIA-SUPER-SECRET" in str(raised.value), "the sample must be one the naive rendering leaks"

    with caplog.at_level(logging.WARNING, logger="ouroboros.mcp.connection"):
        asyncio.run(conn._on_incoming_message(raised.value))

    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "could not parse" in rendered, "the parse failure went unreported"
    assert "AKIA-SUPER-SECRET" not in rendered
    assert "input_value" not in rendered


def test_a_stream_that_simply_ends_is_noticed_on_the_newer_era(monkeypatch):
    """The drop that carries nothing at all: the peer closes the stream and no item arrives.

    This is the other half of the fault path, and the SDK reports it differently — the dispatcher
    leaves its `async for`, marks itself closed and wakes pending requests, but never calls
    `on_stream_exception`. So `message_handler` cannot see it, and on a revision with no keepalive
    the connection would advertise a session that is gone until a tool call failed against it —
    the outcome the spec forbids in as many words.

    A gateway's idle read timeout, a load balancer dropping a held stream and a server restart all
    look like this from here.
    """
    _shrink_timings(monkeypatch)
    app = MCPServer("modern")

    @app.tool()
    def echo() -> str:
        return "pong"

    async def _run():
        async with _reconnectable_fake_server(app) as sessions:
            conn = MCPServerConnection(_placeholder_entry("modern"), unread_egress_auth_dependencies())
            await conn.connect()
            assert conn._session is not None
            assert conn._session.discover_result is not None, "this asserts nothing off the modern era"
            assert len(sessions) == 1

            # Close the server's write end: the client's read stream ends with no item on it.
            (_server_read, server_write), server_task = sessions[0]
            server_task.cancel()
            await server_write.aclose()

            await _wait_until(lambda: len(sessions) == 2 and conn._connected.is_set())
            assert await conn.call_tool(TEST_SCOPE, "echo", {}, timeout_sec=5) == "pong"
            await conn.aclose()

    asyncio.run(_run())


def test_a_transport_fault_retires_the_session_handle_at_once():
    """Ending the cycle is not enough: the handle has to go with it, in the same breath.

    `_await_session` hands back `self._session` whenever it is set, so a fault that only woke the
    supervisor would keep giving callers the dead session until the teardown got round to it — each
    one then waiting out its own timeout there. `_note_dead_session` records that reasoning for the
    call path; it holds identically for the paths that notice without a call, so all three signals
    move together. Asserted with no loop turn in between, because the defect is precisely the gap.
    """
    conn = MCPServerConnection(_placeholder_entry("modern"), unread_egress_auth_dependencies())
    conn._session = SimpleNamespace()  # only its identity matters here
    conn._connected.set()

    asyncio.run(conn._on_incoming_message(httpx2.ReadTimeout("the peer went away")))

    assert conn._session is None
    assert not conn._connected.is_set()
    assert conn._stop_serving.is_set()


def test_the_era_proxy_tracks_the_era(monkeypatch):
    """`discover_result is not None` gates the keepalive, and it must mean "the modern era".

    The property is read instead of the version set because `MODERN_PROTOCOL_VERSIONS` lives in
    `mcp_types`, which `pyproject.toml` does not declare — production code may not import it, a
    test may. Pinning the equivalence keeps that decision from resting on an unstated assumption:
    if the SDK ever set `discover_result` on a revision outside that set, the gate would suppress
    the keepalive on a session that still has `ping`.
    """
    _shrink_timings(monkeypatch)
    app = MCPServer("modern")

    async def _run():
        async with _fake_server(app):
            conn = MCPServerConnection(_placeholder_entry("modern"), unread_egress_auth_dependencies())
            await conn.connect()
            try:
                session = conn._session
                assert (session.discover_result is not None) == (session.protocol_version in MODERN_PROTOCOL_VERSIONS)
            finally:
                await conn.aclose()

    asyncio.run(_run())

    async def _run_handshake():
        async with _handshake_only_server():
            conn = MCPServerConnection(_placeholder_entry("legacy"), unread_egress_auth_dependencies())
            await conn.connect()
            try:
                session = conn._session
                assert (session.discover_result is not None) == (session.protocol_version in MODERN_PROTOCOL_VERSIONS)
            finally:
                await conn.aclose()

    asyncio.run(_run_handshake())


def test_a_reconnect_does_not_repeat_the_era_at_info(monkeypatch, caplog):
    """The ladder starts at one second, so an era record per attempt is a record per second.

    The revision is reported when it is first adopted and whenever it changes; a reconnect that
    negotiated the same one again carries no information, and a server that accepts a connection
    and drops it is exactly the server least worth logging twice a second about.
    """
    _shrink_timings(monkeypatch)
    app = MCPServer("modern")

    @app.tool()
    def echo() -> str:
        return "pong"

    async def _run():
        async with _reconnectable_fake_server(app) as sessions:
            conn = MCPServerConnection(_placeholder_entry("modern"), unread_egress_auth_dependencies())
            await conn.connect()
            (_server_read, server_write), _task = sessions[0]
            await server_write.send(httpx2.ReadTimeout("sse_read_timeout"))
            await _wait_until(lambda: len(sessions) == 2 and conn._connected.is_set())
            await conn.aclose()

    with caplog.at_level(logging.INFO, logger="ouroboros.mcp.connection"):
        asyncio.run(_run())

    spoken = [r.getMessage() for r in caplog.records if "speaks protocol" in r.getMessage()]
    assert len(spoken) == 1, spoken


def test_a_failure_reaching_the_server_is_not_an_era_verdict():
    """An outage must not be read as "this server is old" and answered with a handshake.

    The subject is `negotiate_auto`'s policy — a denylist, where every `MCPError` falls back to the
    handshake and everything else propagates — so the helper is driven for real and only the wire
    call under it is faked. Stubbing the helper itself out would prove no more than that `connect()`
    does not swallow exceptions, and would keep passing against an SDK that caught transport errors
    and downgraded, which is exactly the silent downgrade this pins against.
    """
    app = MCPServer("unreachable")
    handshakes: list[str] = []

    async def _no_route(_self, _version):
        raise httpx2.ConnectError("no route to host")

    async def _record_handshake(_self):
        handshakes.append("initialize")
        raise AssertionError("a transport fault was answered with a handshake")

    async def _run():
        async with _fake_server(app):
            with (
                patch.object(ClientSession, "send_discover", _no_route),
                patch.object(ClientSession, "initialize", _record_handshake),
            ):
                conn = MCPServerConnection(_placeholder_entry("unreachable"), unread_egress_auth_dependencies())
                with pytest.raises(httpx2.ConnectError, match="no route to host"):
                    await conn.connect()

    asyncio.run(_run())

    assert handshakes == [], "the probe's transport fault was downgraded to the handshake era"


def test_the_sdk_still_exposes_the_negotiation_helper():
    """`negotiate_auto` is private to the SDK, and this module imports it at module scope.

    `mcp.client.__all__` does not carry it, and the leading underscore on `_probe` reserves the
    right to move it. Pinned here so an SDK bump that does fails a test, rather than failing
    `import ouroboros.mcp.connection` — which composition performs at startup — and taking the whole
    process down instead of one server.
    """
    from mcp.client import _probe

    assert inspect.iscoroutinefunction(_probe.negotiate_auto)
    assert connection_mod.negotiate_auto is _probe.negotiate_auto


def test_the_sdk_still_exposes_the_stream_pair_type():
    """`mcp.client._transport` is private, and `transports.py` aliases it at module scope.

    Aliased rather than restated so one contract has one definition — `ClientSession` is typed
    against this pair, and a local `tuple[ReadStream[...], WriteStream[...]]` would go silently
    wrong on a bump that changed either element *or the pairing*. Pinned for the same reason
    `negotiate_auto` is: a move should fail here rather than at
    `import ouroboros.mcp.transports`, which composition performs at startup.
    """
    from mcp.client import _transport

    assert transports_mod.MCPStreams is _transport.TransportStreams


def test_the_narrowed_timeout_keeps_every_budget_but_connect():
    """`_http_client` is typed as the SDK's generic `McpHttpClientFactory`, and callers differ.

    Only `connect` is this package's to impose — it is what makes `TCP_CONNECT_TIMEOUT_SEC` mean the
    same thing on both transports, and `connection.py` derives its start-up budget from it. A
    caller's write or pool budget replaced silently would be a second, unstated policy on a client
    this module only decorates.
    """
    narrowed = transports_mod._narrowed_timeout(
        httpx2.Timeout(connect=transports_mod.TCP_CONNECT_TIMEOUT_SEC + 20.0, read=2.0, write=3.0, pool=4.0)
    )

    assert narrowed.connect == transports_mod.TCP_CONNECT_TIMEOUT_SEC
    assert (narrowed.read, narrowed.write, narrowed.pool) == (2.0, 3.0, 4.0)


def test_the_narrowed_timeout_does_not_widen_a_shorter_connect_budget():
    """Narrowed, not replaced: a caller asking for less has no policy of this module's overruled.

    The one caller today hands in more, so the direction only happens to be right — and a helper
    named for narrowing that silently multiplies a 1s budget by ten is the kind of thing the next
    caller inherits without reading.
    """
    narrowed = transports_mod._narrowed_timeout(httpx2.Timeout(connect=1.0, read=2.0, write=3.0, pool=4.0))

    assert narrowed.connect == 1.0


def test_the_narrowed_timeout_fills_in_what_the_caller_left_unset():
    """`sse_client` spreads one scalar and leaves nothing None, but the protocol allows it."""
    narrowed = transports_mod._narrowed_timeout(httpx2.Timeout(None))

    assert narrowed.connect == transports_mod.TCP_CONNECT_TIMEOUT_SEC
    assert narrowed.read == transports_mod.get_mcp_stream_read_timeout_sec()
    assert narrowed.write == transports_mod._HTTP_TIMEOUT_SEC
    assert narrowed.pool == transports_mod._HTTP_TIMEOUT_SEC


def test_the_connect_budget_covers_the_negotiation_the_sdk_can_spend():
    """The negotiation spends the connect budget, and a `connect()` timeout is not retried.

    `MCPManager.initialize` logs a server whose connect timed out and skips it for the life of the
    process — there is no reconnect ladder behind it, unlike a mid-life drop. The SDK's own timeouts
    are fixed and unreachable from here, so the budget is sized around them or a server that used to
    fit stops connecting the next time one of them is raised.

    The whole negotiation, not one probe and not one handshake: the loop in `mcp/client/_probe.py`
    runs **twice**, and each pass can spend a `server/discover` *and* an `initialize`. A -32022
    answered to the fallback handshake is itself modern evidence, so it re-probes at a mutual
    version — and that second probe can fall back to a second handshake in turn. Counting one
    `initialize` for the pair left the first `tools/list` nothing at all.

    The budget is computed from those spends rather than written down, so what is left to pin is
    the one input production cannot read at runtime: `_PROBE_PASSES` is a `range(...)` literal in
    the SDK's source, and only a regex over that source can say it is still 2. The recomputation
    below is not the formula restated for its own sake — it fails if someone edits the production
    expression to drop the trailing `tools/list` term, which is exactly the edit that looks
    harmless.
    """
    from mcp.client.session import DISCOVER_TIMEOUT_SECONDS

    passes = _probe_loop_passes()
    assert passes == connection_mod._PROBE_PASSES, (
        f"`negotiate_auto` now makes {passes} probe/handshake passes, not "
        f"{connection_mod._PROBE_PASSES}; the connect budget is computed from that count"
    )
    negotiation = transports_mod.TCP_CONNECT_TIMEOUT_SEC + passes * (  # opening the transport
        DISCOVER_TIMEOUT_SECONDS  # the probe this pass sends
        + connection_mod.get_mcp_request_timeout_sec()  # the `initialize` it can fall back to
    )
    assert negotiation + connection_mod.get_mcp_request_timeout_sec() <= connection_mod._connect_budget_sec(), (
        f"the connect budget ({connection_mod._connect_budget_sec()}s) must cover the whole "
        f"negotiation ({passes} probe/handshake passes, {negotiation}s) and still leave one full "
        f"request ({connection_mod.get_mcp_request_timeout_sec()}s) for the first tool listing"
    )
    # Still not the absolute worst case, and deliberately not: on `sse` the transport is not open
    # until the server sends its `endpoint` event, a wait bounded by `MCP_STREAM_READ_TIMEOUT_SEC`
    # (300s) rather than by the connect budget, and a paginated listing has no bound at all. What
    # closes both is a reconnect ladder behind `connect()` rather than a bigger constant here.
    # Deferred by name in the change's tasks; this pins that the SDK's own fixed per-request
    # spends — the part that grows silently on a bump — cannot eat the budget.


def _probe_loop_passes() -> int:
    """How many times `negotiate_auto`'s loop can run, read off the SDK rather than assumed.

    The count is a `range(...)` literal in `mcp/client/_probe.py`, reachable no other way: the
    function returns after one pass on every path a test can drive, so nothing observable says
    two. An SDK that widens the loop would otherwise leave the budget below its own worst case
    with this suite green — which is exactly how the previous value came to equal it.
    """
    from mcp.client import _probe

    counts = re.findall(r"for attempt in range\((\d+)\)", inspect.getsource(_probe.negotiate_auto))
    assert counts == ["2"], f"negotiate_auto no longer loops a literal number of times: {counts}"
    return int(counts[0])


# ---------------------------------------------------------------------------
# MCPManager — singleton lifecycle, discovery index, sync/async bridging
# ---------------------------------------------------------------------------


def test_manager_before_initialize_is_empty():
    manager = MCPManager()
    assert manager.discovered_tools() == []
    assert manager.resolve("mcp__anything__tool") is None
    assert manager.is_available() is False


def test_initialize_twice_raises():
    async def _run():
        manager = MCPManager()
        loop = asyncio.get_running_loop()
        await manager.initialize([], loop, unread_egress_auth_dependencies())
        with pytest.raises(RuntimeError, match="already called"):
            await manager.initialize([], loop, unread_egress_auth_dependencies())

    asyncio.run(_run())


def test_initialize_skips_unreachable_server():
    """The subject is the manager dropping a server whose `connect()` raised.

    The transport is patched rather than pointed at a closed port: a port bound to learn its
    number and released is another process's to take, and the test would then pass or fail for a
    reason that has nothing to do with the manager.
    """

    @asynccontextmanager
    async def _always_fails(entry, _forwarded_headers):
        raise RuntimeError("no route to host")
        yield  # pragma: no cover

    async def _run():
        manager = MCPManager()
        loop = asyncio.get_running_loop()
        with patch.object(connection_mod, "open_transport", _always_fails):
            await manager.initialize([_placeholder_entry("down")], loop, unread_egress_auth_dependencies())
            assert manager.discovered_tools() == []
            assert manager._connections == {}

    asyncio.run(_run())


def test_initialize_ignores_disabled_servers():
    """The `enabled` filter is the subject, so an attempt to open the entry fails the test.

    Asserting on `_connections` alone cannot tell the filter from a connection that was opened and
    then dropped, and an unpatched transport would answer the question over the network.
    """
    opened: list[str] = []

    @asynccontextmanager
    async def _record_and_fail(entry, _forwarded_headers):
        opened.append(entry.id)
        raise RuntimeError("a disabled server must never be opened")
        yield  # pragma: no cover

    async def _run():
        manager = MCPManager()
        loop = asyncio.get_running_loop()
        with patch.object(connection_mod, "open_transport", _record_and_fail):
            await manager.initialize(
                [_placeholder_entry("off", enabled=False)], loop, unread_egress_auth_dependencies()
            )
            assert opened == []
            assert manager._connections == {}

    asyncio.run(_run())


def test_initialize_builds_tool_index():
    app = MCPServer("indexed")

    @app.tool()
    def ping() -> str:
        return "pong"

    async def _run():
        async with _fake_server(app):
            manager = MCPManager()
            entry = _placeholder_entry(
                "indexed",
                retrieval_aliases=["pulse", "пульс"],
                tool_result_limits={"ping": 30_000},
            )
            loop = asyncio.get_running_loop()
            await manager.initialize([entry], loop, unread_egress_auth_dependencies())

            assert [t.full_name for t in manager.discovered_tools()] == ["mcp__indexed__ping"]
            assert manager.resolve("mcp__indexed__ping") == ("indexed", "ping")
            assert manager.retrieval_aliases("mcp__indexed__ping") == ("pulse", "пульс")
            assert manager.retrieval_aliases("mcp__other__ping") == ()
            assert manager.tool_result_limit("mcp__indexed__ping") == 30_000
            assert manager.tool_result_limit("mcp__other__ping") is None
            assert manager.resolve("mcp__indexed__missing") is None
            assert manager.resolve("not_an_mcp_tool") is None

    asyncio.run(_run())


def test_initialize_isolates_server_metadata(monkeypatch):
    async def _run():
        manager = MCPManager()

        async def _connect(entry, _auth_dependencies):
            manager._connections[entry.id] = SimpleNamespace(
                entry=entry,
                tools=[
                    DiscoveredMCPTool(
                        server_id=entry.id,
                        original_name=original_name,
                        full_name=_build_mcp_tool_name(entry.id, original_name),
                        schema=ToolSchema(
                            name=_build_mcp_tool_name(entry.id, original_name),
                            description=original_name,
                            parameters={},
                        ),
                    )
                    for original_name in ("mcp_query", "get_overview")
                ],
            )

        monkeypatch.setattr(manager, "_connect_one", _connect)
        loop = asyncio.get_running_loop()
        await manager.initialize(
            [
                _placeholder_entry(
                    "hr-provider",
                    retrieval_aliases=["pulse", "пульс"],
                    tool_result_limits={"mcp_query": 30_000},
                ),
                _placeholder_entry("other-provider"),
            ],
            loop,
            unread_egress_auth_dependencies(),
        )

        assert manager.retrieval_aliases("mcp__hr-provider__mcp_query") == ("pulse", "пульс")
        assert manager.retrieval_aliases("mcp__other-provider__mcp_query") == ()
        assert manager.tool_result_limit("mcp__hr-provider__mcp_query") == 30_000
        assert manager.tool_result_limit("mcp__hr-provider__get_overview") is None
        assert manager.tool_result_limit("mcp__other-provider__mcp_query") is None

    asyncio.run(_run())


def test_shutdown_closes_connections_from_an_interrupted_initialize(monkeypatch):
    async def _run():
        manager = MCPManager()
        connected = asyncio.Event()
        closed: list[str] = []

        class _PartialConnection:
            async def aclose(self) -> None:
                closed.append("partial")

        async def _connect_then_hang(entry, _auth_dependencies):
            manager._connections[entry.id] = _PartialConnection()
            connected.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(manager, "_connect_one", _connect_then_hang)
        task = asyncio.create_task(
            manager.initialize(
                [_placeholder_entry("partial")], asyncio.get_running_loop(), unread_egress_auth_dependencies()
            )
        )
        await connected.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert manager._initialized is False
        await manager.shutdown()
        assert closed == ["partial"]
        assert manager._connections == {}

    asyncio.run(_run())


class _FakeConnection:
    def __init__(self, *, delay: float = 0.0, result: str = "ok", raise_exc: Exception | None = None):
        self._delay = delay
        self._result = result
        self._raise_exc = raise_exc
        self.calls: list[tuple[str, dict, float]] = []
        self.tools: list[DiscoveredMCPTool] = []
        self.scopes: list[object] = []

    async def call_tool(self, scope, name, arguments, timeout_sec):
        self.calls.append((name, arguments, timeout_sec))
        self.scopes.append(scope)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raise_exc:
            raise self._raise_exc
        return self._result


def test_call_tool_sync_bridges_to_background_loop(background_loop):
    manager = MCPManager(background_loop)
    fake = _FakeConnection(result="pong")
    manager._connections["srv"] = fake

    assert threading.current_thread() is threading.main_thread()
    result = manager.call_tool_sync(TEST_SCOPE, "srv", "ping", {"a": 1}, timeout_sec=5)

    assert result == "pong"
    assert fake.calls == [("ping", {"a": 1}, 5)]
    assert fake.scopes == [TEST_SCOPE]


def test_call_tool_sync_unknown_server(background_loop):
    manager = MCPManager(background_loop)
    result = manager.call_tool_sync(TEST_SCOPE, "missing", "ping", {}, timeout_sec=5)
    assert "unknown MCP server" in result


def test_call_tool_sync_times_out_and_cancels(background_loop):
    # call_tool_sync gives the outer, cross-thread wait a grace margin over
    # timeout_sec so a well-behaved connection's own read-timeout error wins;
    # this fake ignores timeout_sec entirely, so it must out-hang that margin
    # for the outer safety-net timeout to be the one that fires.
    manager = MCPManager(background_loop)
    fake = _FakeConnection(delay=3.0)
    manager._connections["srv"] = fake

    result = manager.call_tool_sync(TEST_SCOPE, "srv", "slow", {}, timeout_sec=0.1)

    assert result == "⚠️ MCP_TOOL_ERROR (slow): timed out after 0.1s"


def test_call_tool_sync_propagates_errors(background_loop):
    manager = MCPManager(background_loop)
    fake = _FakeConnection(raise_exc=RuntimeError("boom"))
    manager._connections["srv"] = fake

    result = manager.call_tool_sync(TEST_SCOPE, "srv", "bad", {}, timeout_sec=5)
    assert result == "⚠️ MCP_TOOL_ERROR (bad): boom"


def test_mcp_tool_completes_through_the_catalog_bridge(background_loop, tmp_path, monkeypatch):
    """The whole path, on the shape production has: a worker thread reaches the round's
    synchronous bridge, which runs a private loop of its own, and the tool body inside it
    waits on a coroutine belonging to the *main* loop.

    This is what the bridge is shaped to survive. Scheduling the round onto that main loop
    instead — the repo's habitual ``run_coroutine_threadsafe`` — would deadlock here, with
    ``call_tool_sync`` waiting on the loop it was already running on, and no unit test of
    the runtimes alone would show it.

    The bridge moved up a level: it used to be ``ToolCatalog.execute``, once per tool call,
    and is now ``handle_tool_calls``, once per round. ``call_tool_sync`` stays until the
    caller itself runs on the main loop — MCP sessions are loop-affine — so this stays the
    deadlock check across that move.
    """
    from concurrent.futures import ThreadPoolExecutor

    from new_core.llm.types import ToolCall, ToolCallFunction
    from new_core.tools.providers.mcp import McpToolProvider
    from new_core.tools.schema import ToolSchema
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from ouroboros.policy import safety
    from tests.helpers.shared import collecting_event_sink, make_catalog, make_tool_ctx

    # The round runs the dispatch gates too; an unconfigured safety backend would prepend a
    # warning to every result and this test is about the loop hand-off, not the gates.
    monkeypatch.setattr(safety, "check_safety", permissive_safety)

    class _AvailableManager(MCPManager):
        # `is_available()` also reads the live MCP_ENABLED setting; the kill switch is not
        # what this test is about.
        def is_available(self) -> bool:
            return True

    manager = _AvailableManager(background_loop)
    fake = _FakeConnection(result="pong")
    manager._connections["srv"] = fake
    manager._tool_index["mcp__srv__ping"] = DiscoveredMCPTool(
        server_id="srv",
        original_name="ping",
        full_name="mcp__srv__ping",
        schema=ToolSchema(name="mcp__srv__ping", description="", parameters={}),
    )
    manager._initialized = True

    catalog = make_catalog(
        [
            McpToolProvider(
                servers=[SimpleNamespace(id="srv")],
                server_ids_reader=StubMcpServerIdsReader(["srv"]),
                auth_dependencies=null_egress_auth_dependencies(),
                manager=manager,
                timeout_reader=lambda: 5,
            )
        ]
    )
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)

    calls = [ToolCall(id="c0", function=ToolCallFunction(name="mcp__srv__ping", arguments=json.dumps({"a": 1})))]

    executor = build_tool_call_executor(
        confirmation_gate=False,
        catalog=catalog,
        events=collecting_event_sink(),
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ouroboros-task") as pool:
        results = pool.submit(
            executor.execute,
            calls,
            ctx=ctx,
            task_id="task-mcp",
            decisions={},
        ).result(timeout=10)

    assert [r["result"] for r in results] == ["pong"]
    assert fake.calls == [("ping", {"a": 1}, 5)]
    assert fake.scopes == [ctx.scope]


def test_a_session_runs_under_an_operation_it_minted_for_itself():
    """A reconnect during a task must not stamp that task's id on the next hour of pings.

    So the session's operation is minted, never inherited from whatever was ambient when the
    connection happened to be (re)established — the frozen-identifier trap, one layer up.
    """

    app = MCPServer("greeter")

    @app.tool()
    def greet(name: str) -> str:
        """Say hello."""
        return f"Hello, {name}!"

    during_connect: list[str] = []
    during_call: list[str] = []
    unrelated = "463ac35c-9f64-13ad-4848-5a3953bb6124"

    async def _run():
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            server_read, server_write = server_streams
            task = asyncio.create_task(
                app._lowlevel_server.run(
                    server_read, server_write, app._lowlevel_server.create_initialization_options()
                )
            )

            @asynccontextmanager
            async def _fake_open_transport(entry, _forwarded_headers):
                # Inside the session's scope: this is the frame the pump task inherits from.
                during_connect.append(aef_tracing.current_trace_id())
                yield client_streams

            with patch.object(connection_mod, "open_transport", _fake_open_transport):
                try:
                    entry = _placeholder_entry("greeter")
                    conn = MCPServerConnection(entry, unread_egress_auth_dependencies())
                    with aef_tracing.trace_scope(unrelated):
                        await conn.connect()
                    caller = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
                    with aef_tracing.trace_scope(caller):
                        during_call.append(aef_tracing.current_trace_id())
                        assert (
                            await conn.call_tool(TEST_SCOPE, "greet", {"name": "World"}, timeout_sec=5)
                            == "Hello, World!"
                        )
                    await conn.aclose()
                finally:
                    task.cancel()

    asyncio.run(_run())

    assert during_connect, "the transport was never opened"
    session_trace_id = during_connect[0]
    assert session_trace_id not in ("", unrelated), "the session inherited instead of minting"
    assert aef_tracing.normalize_trace_id(session_trace_id) == session_trace_id
    # The caller's own operation is untouched by the connection's.
    assert during_call == ["f47ac10b-58cc-4372-a567-0e02b2c3d479"]


def test_a_session_names_the_operation_that_asked_for_it(monkeypatch, caplog):
    """A minted identifier is not a lost one: the caller is named beside it, never adopted.

    Only the first session has a caller to name. `_supervise` is a Task created by `connect()`,
    so the identifier ambient inside it is frozen there; naming it on a reconnect would rebuild
    the frozen-identifier confusion inside the record written to resolve it.
    """
    _shrink_timings(monkeypatch)
    app = MCPServer("greeter")

    @app.tool()
    def greet() -> str:
        """Say hello."""
        return "Hello!"

    caller = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    async def _run():
        async with _reconnectable_fake_server(app) as sessions:
            conn = MCPServerConnection(_placeholder_entry("greeter"), unread_egress_auth_dependencies())
            with aef_tracing.trace_scope(caller):
                await conn.connect()
            (_server_read, server_write), _task = sessions[0]
            await server_write.send(httpx2.ReadTimeout("sse_read_timeout"))
            await _wait_until(lambda: len(sessions) == 2 and conn._connected.is_set())
            await conn.aclose()

    with caplog.at_level(logging.INFO, logger="ouroboros.mcp.connection"):
        asyncio.run(_run())

    # `r.msg`, not the rendered message: the format string is what identifies the record, so a
    # server whose id happens to contain the phrase cannot pass this filter on its own.
    opened = [r.getMessage() for r in caplog.records if r.msg.startswith("Opening an MCP session")]
    assert len(opened) == 2, opened
    assert f"source_trace_id={caller}" in opened[0]
    assert "source_trace_id" not in opened[1], "a reconnect named an operation that never asked for it"
