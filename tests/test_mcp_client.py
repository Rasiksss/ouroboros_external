"""Tests for the MCP client — real-SDK connection integration + manager bridging.

Async bodies are driven via asyncio.run(), matching this repo's existing
convention (see tests/test_event_bus.py, tests/conftest.py) rather than
pytest-asyncio, which isn't a project dependency.
"""

from __future__ import annotations

import asyncio
import re
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp.types import (
    LATEST_PROTOCOL_VERSION,
    ErrorData,
    Implementation,
    InitializeResult,
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCResponse,
    ListToolsResult,
    ServerCapabilities,
    Tool,
)

import ouroboros.mcp.connection as connection_mod
import ouroboros.mcp.transports as transports_mod
from new_core.tools.schema import ToolSchema
from ouroboros.mcp.connection import (
    DiscoveredMCPTool,
    MCPServerConnection,
    _build_mcp_tool_name,
)
from ouroboros.mcp.manager import MCPManager
from ouroboros.mcp.transports import _auth_headers, _merged_env, open_transport
from ouroboros.storage.settings.schema import McpServerEntry
from ouroboros.tools.naming import sanitize_tool_name

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


_PROVIDER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


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


def test_auth_headers_requires_both_fields():
    entry = McpServerEntry(id="s", auth_header="X-Key")
    assert _auth_headers(entry) == {}
    entry2 = McpServerEntry(id="s", auth_header="X-Key", auth_token="secret")
    assert _auth_headers(entry2) == {"X-Key": "secret"}


def test_mcp_server_optional_metadata_defaults_are_empty():
    entry = McpServerEntry(id="s")

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
        McpServerEntry(id="s", tool_result_limits=tool_result_limits)


def test_merged_env_preserves_defaults():
    merged = _merged_env({"CUSTOM": "1"})
    assert merged["CUSTOM"] == "1"
    assert "PATH" in merged  # default environment must not be dropped


def test_open_transport_unsupported():
    async def _run():
        # transport is a validated Literal, so bypass validation to exercise
        # the defensive runtime branch directly.
        entry = McpServerEntry.model_construct(id="s", transport="carrier-pigeon")
        with pytest.raises(ValueError, match="Unsupported MCP transport"):
            async with open_transport(entry):
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
            auth_header="X-Key",
            auth_token="secret",
        )
        with patch.object(transports_mod, "streamable_http_client", _fake_streamable_http_client):
            async with open_transport(entry) as streams:
                assert streams == (None, None)

    asyncio.run(_run())

    client = captured["client"]
    assert captured["url"] == "https://example.test/mcp"
    assert client.headers["X-Key"] == "secret"
    assert client.follow_redirects is True
    assert client.timeout.read >= 300.0  # longer than any tool call this app makes
    assert client.timeout.connect == 10.0
    assert client.timeout.write == 30.0
    assert client.timeout.pool == 30.0


def test_sse_transport_sets_explicit_http_timeouts():
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def _fake_sse_client(url, headers=None, timeout=None, sse_read_timeout=None):
        captured.update(url=url, headers=headers, timeout=timeout, sse_read_timeout=sse_read_timeout)
        yield (None, None)

    async def _run():
        entry = McpServerEntry(id="s", transport="sse", url="https://example.test/sse")
        with patch.object(transports_mod, "sse_client", _fake_sse_client):
            async with open_transport(entry) as streams:
                assert streams == (None, None)

    asyncio.run(_run())

    assert captured["timeout"] == 30.0
    assert captured["sse_read_timeout"] >= 300.0


# ---------------------------------------------------------------------------
# MCPServerConnection — real SDK, in-memory transport (no network/subprocess)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _fake_server(app: FastMCP):
    """Run `app` against an in-memory transport and patch open_transport to reach it."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        server_read, server_write = server_streams
        task = asyncio.create_task(
            app._mcp_server.run(server_read, server_write, app._mcp_server.create_initialization_options())
        )

        @asynccontextmanager
        async def _fake_open_transport(entry):
            yield client_streams

        with patch.object(connection_mod, "open_transport", _fake_open_transport):
            try:
                yield
            finally:
                task.cancel()


def test_connection_connect_lists_and_calls_tool():
    app = FastMCP("greeter")

    @app.tool()
    def greet(name: str) -> str:
        """Say hello."""
        return f"Hello, {name}!"

    async def _run():
        async with _fake_server(app):
            entry = McpServerEntry(id="greeter", transport="stdio", command="unused")
            conn = MCPServerConnection(entry)
            await conn.connect()

            assert [t.full_name for t in conn.tools] == ["mcp__greeter__greet"]
            assert conn.tools[0].original_name == "greet"
            assert "[MCP greeter]" in conn.tools[0].schema.description

            result = await conn.call_tool("greet", {"name": "World"}, timeout_sec=5)
            assert result == "Hello, World!"
            await conn.aclose()

    asyncio.run(_run())


def test_connect_shuts_down_session_task_when_discovery_fails():
    app = FastMCP("greeter")

    @app.tool()
    def greet(name: str) -> str:
        """Say hello."""
        return f"Hello, {name}!"

    async def _boom(self):
        raise RuntimeError("discovery failed")

    async def _run():
        async with _fake_server(app):
            entry = McpServerEntry(id="greeter", transport="stdio", command="unused")
            conn = MCPServerConnection(entry)
            with patch.object(MCPServerConnection, "_refresh_tools", _boom):
                with pytest.raises(RuntimeError, match="discovery failed"):
                    await conn.connect()

            assert conn._session_task is not None
            assert conn._session_task.done()
            assert conn._session is None

    asyncio.run(_run())


def test_connection_call_tool_error_is_formatted():
    app = FastMCP("boomer")

    @app.tool()
    def boom() -> str:
        raise ValueError("kaboom")

    async def _run():
        async with _fake_server(app):
            entry = McpServerEntry(id="boomer", transport="stdio", command="unused")
            conn = MCPServerConnection(entry)
            await conn.connect()

            result = await conn.call_tool("boom", {}, timeout_sec=5)
            assert result.startswith("⚠️ MCP_TOOL_ERROR (boom):")
            assert "kaboom" in result
            await conn.aclose()

    asyncio.run(_run())


def test_connection_call_tool_native_timeout():
    app = FastMCP("slowpoke")

    @app.tool()
    async def slow() -> str:
        await asyncio.sleep(2)
        return "done"

    async def _run():
        async with _fake_server(app):
            entry = McpServerEntry(id="slowpoke", transport="stdio", command="unused")
            conn = MCPServerConnection(entry)
            await conn.connect()

            result = await conn.call_tool("slow", {}, timeout_sec=0.2)
            assert result.startswith("⚠️ MCP_TOOL_ERROR (slow):")
            await conn.aclose()

    asyncio.run(_run())


def test_connection_allowed_tools_filters_discovery():
    app = FastMCP("multi")

    @app.tool()
    def a_tool() -> str:
        return "a"

    @app.tool()
    def b_tool() -> str:
        return "b"

    async def _run():
        async with _fake_server(app):
            entry = McpServerEntry(id="multi", transport="stdio", command="unused", allowed_tools=["a_tool"])
            conn = MCPServerConnection(entry)
            await conn.connect()

            assert [t.original_name for t in conn.tools] == ["a_tool"]
            await conn.aclose()

    asyncio.run(_run())


def test_refresh_tools_follows_pagination():
    entry = McpServerEntry(id="paged", transport="stdio", command="unused")
    conn = MCPServerConnection(entry)

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
        conn._session = _FakeSession()
        await conn._refresh_tools()

    asyncio.run(_run())

    assert calls == [None, "page2"]
    assert [t.original_name for t in conn.tools] == ["one", "two"]


# ---------------------------------------------------------------------------
# MCPServerConnection — surviving a dropped session
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _reconnectable_fake_server(app: FastMCP):
    """Like `_fake_server`, but each open_transport() gets a fresh in-memory server, and the
    server side of each is handed to the test so it can be killed. Yields them, newest last."""
    sessions: list[tuple] = []

    @asynccontextmanager
    async def _fake_open_transport(entry):
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            task = asyncio.create_task(
                app._mcp_server.run(
                    server_streams[0], server_streams[1], app._mcp_server.create_initialization_options()
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
    monkeypatch.setattr(connection_mod, "_SESSION_READ_TIMEOUT_SEC", 0.5)


_FAKE_RESULTS = {
    "initialize": InitializeResult(
        protocolVersion=LATEST_PROTOCOL_VERSION,
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
            root = getattr(getattr(message, "message", None), "root", None)
            request_id = getattr(root, "id", None)
            if request_id is None:
                continue  # a notification: nothing to answer
            if dead.is_set():
                payload = JSONRPCError(
                    jsonrpc="2.0",
                    id=request_id,
                    error=ErrorData(code=32600, message="Session terminated"),
                )
            else:
                payload = JSONRPCResponse(jsonrpc="2.0", id=request_id, result=_FAKE_RESULTS[root.method])
            await write_stream.send(SessionMessage(JSONRPCMessage(payload)))

    @asynccontextmanager
    async def _fake_open_transport(entry):
        index = len(opens)
        opens.append(index)
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            dead = terminate if index == 0 else asyncio.Event()
            task = asyncio.create_task(_serve(server_streams[0], server_streams[1], dead))
            try:
                yield client_streams
            finally:
                task.cancel()

    with patch.object(connection_mod, "open_transport", _fake_open_transport):
        yield opens, terminate


def test_the_idle_ping_notices_a_session_the_client_wrote_off(monkeypatch):
    """The SDK turns a 404 into an error *reply*; reading that as proof of life wedges forever."""
    _shrink_timings(monkeypatch)

    async def _run():
        async with _terminated_session_server() as (opens, terminate):
            conn = MCPServerConnection(McpServerEntry(id="gone", transport="stdio", command="unused"))
            await conn.connect()
            assert opens == [0]

            terminate.set()

            await _wait_until(lambda: len(opens) == 2 and conn._connected.is_set())
            assert await conn.call_tool("echo", {}, timeout_sec=5) == "pong"
            await conn.aclose()

    asyncio.run(_run())


def test_call_tool_notices_a_session_the_client_wrote_off(monkeypatch):
    _shrink_timings(monkeypatch, healthcheck=30.0)  # no ping will fire; only the call can tell

    async def _run():
        async with _terminated_session_server() as (opens, terminate):
            conn = MCPServerConnection(McpServerEntry(id="gone", transport="stdio", command="unused"))
            await conn.connect()

            terminate.set()
            failed = await conn.call_tool("echo", {}, timeout_sec=5)
            assert "Session terminated" in failed
            # The next call must not be handed the same dead session; it would hang there.
            assert conn._session is None
            assert not conn._connected.is_set()

            await _wait_until(lambda: len(opens) == 2 and conn._connected.is_set())
            assert await conn.call_tool("echo", {}, timeout_sec=5) == "pong"
            await conn.aclose()

    asyncio.run(_run())


def test_connection_reconnects_after_a_silent_drop(monkeypatch):
    """The drop that raises nothing: the server side goes away and only the idle ping can tell."""
    app = FastMCP("flaky")

    @app.tool()
    def echo() -> str:
        return "pong"

    _shrink_timings(monkeypatch)

    async def _run():
        async with _reconnectable_fake_server(app) as sessions:
            conn = MCPServerConnection(McpServerEntry(id="flaky", transport="stdio", command="unused"))
            await conn.connect()
            assert len(sessions) == 1
            assert await conn.call_tool("echo", {}, timeout_sec=5) == "pong"

            (_server_read, server_write), server_task = sessions[0]
            server_task.cancel()
            await server_write.aclose()

            await _wait_until(lambda: len(sessions) == 2 and conn._connected.is_set())
            assert await conn.call_tool("echo", {}, timeout_sec=5) == "pong"
            assert conn._inflight == 0
            await conn.aclose()

    asyncio.run(_run())


def test_call_tool_reconnects_when_the_session_streams_are_gone(monkeypatch):
    app = FastMCP("flaky")

    @app.tool()
    def echo() -> str:
        return "pong"

    _shrink_timings(monkeypatch)

    async def _run():
        async with _reconnectable_fake_server(app) as sessions:
            conn = MCPServerConnection(McpServerEntry(id="flaky", transport="stdio", command="unused"))
            await conn.connect()

            (server_read, _server_write), server_task = sessions[0]
            server_task.cancel()
            await server_read.aclose()  # the client's next send has nowhere to go

            failed = await conn.call_tool("echo", {}, timeout_sec=5)
            assert failed.startswith("⚠️ MCP_TOOL_ERROR (echo):")
            assert conn._session is None

            await _wait_until(lambda: len(sessions) == 2 and conn._connected.is_set())
            assert await conn.call_tool("echo", {}, timeout_sec=5) == "pong"
            assert conn._inflight == 0
            await conn.aclose()

    asyncio.run(_run())


def test_first_connect_failure_is_reported_and_not_retried(monkeypatch):
    """A server that never came up is the manager's to drop, not this class's to retry."""
    monkeypatch.setattr(connection_mod, "_RECONNECT_MIN_DELAY_SEC", 0.01)
    attempts: list[str] = []

    @asynccontextmanager
    async def _always_fails(entry):
        attempts.append(entry.id)
        raise RuntimeError("no route to host")
        yield  # pragma: no cover

    async def _run():
        with patch.object(connection_mod, "open_transport", _always_fails):
            conn = MCPServerConnection(McpServerEntry(id="down", transport="stdio", command="unused"))
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
    async def _open_once_then_fail(entry):
        opens.append(entry.id)
        if len(opens) > 1:
            raise RuntimeError("still down")
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            app = FastMCP("brief")
            task = asyncio.create_task(
                app._mcp_server.run(
                    server_streams[0], server_streams[1], app._mcp_server.create_initialization_options()
                )
            )
            try:
                yield client_streams
            finally:
                task.cancel()

    async def _run():
        with patch.object(connection_mod, "open_transport", _open_once_then_fail):
            conn = MCPServerConnection(McpServerEntry(id="brief", transport="stdio", command="unused"))
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
    async def _hangs_on_exit(entry):
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            app = FastMCP("stuck")
            task = asyncio.create_task(
                app._mcp_server.run(
                    server_streams[0], server_streams[1], app._mcp_server.create_initialization_options()
                )
            )
            try:
                yield client_streams
            finally:
                task.cancel()
                await asyncio.sleep(30)  # the goodbye that never comes back

    async def _run():
        with patch.object(connection_mod, "open_transport", _hangs_on_exit):
            conn = MCPServerConnection(McpServerEntry(id="stuck", transport="stdio", command="unused"))
            await conn.connect()

            loop = asyncio.get_running_loop()
            started = loop.time()
            await asyncio.wait_for(conn.aclose(), timeout=5)
            waited = loop.time() - started

            assert waited < 2.0, f"aclose() sat on a hung teardown for {waited:.1f}s"
            assert conn._session_task.done()

    asyncio.run(_run())


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
        await manager.initialize([], loop)
        with pytest.raises(RuntimeError, match="already called"):
            await manager.initialize([], loop)

    asyncio.run(_run())


def test_initialize_skips_unreachable_server():
    async def _run():
        manager = MCPManager()
        loop = asyncio.get_running_loop()
        bad_entry = McpServerEntry(id="down", transport="stdio", command="/no/such/binary-xyz")
        await manager.initialize([bad_entry], loop)
        assert manager.discovered_tools() == []
        assert manager._connections == {}

    asyncio.run(_run())


def test_initialize_ignores_disabled_servers():
    async def _run():
        manager = MCPManager()
        loop = asyncio.get_running_loop()
        disabled = McpServerEntry(id="off", enabled=False, transport="stdio", command="/no/such/binary-xyz")
        await manager.initialize([disabled], loop)
        assert manager._connections == {}

    asyncio.run(_run())


def test_initialize_builds_tool_index():
    app = FastMCP("indexed")

    @app.tool()
    def ping() -> str:
        return "pong"

    async def _run():
        async with _fake_server(app):
            manager = MCPManager()
            entry = McpServerEntry(
                id="indexed",
                transport="stdio",
                command="unused",
                retrieval_aliases=["pulse", "пульс"],
                tool_result_limits={"ping": 30_000},
            )
            loop = asyncio.get_running_loop()
            await manager.initialize([entry], loop)

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

        async def _connect(entry):
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
                McpServerEntry(
                    id="hr-provider",
                    retrieval_aliases=["pulse", "пульс"],
                    tool_result_limits={"mcp_query": 30_000},
                ),
                McpServerEntry(id="other-provider"),
            ],
            loop,
        )

        assert manager.retrieval_aliases("mcp__hr-provider__mcp_query") == ("pulse", "пульс")
        assert manager.retrieval_aliases("mcp__other-provider__mcp_query") == ()
        assert manager.tool_result_limit("mcp__hr-provider__mcp_query") == 30_000
        assert manager.tool_result_limit("mcp__hr-provider__get_overview") is None
        assert manager.tool_result_limit("mcp__other-provider__mcp_query") is None

    asyncio.run(_run())


class _FakeConnection:
    def __init__(self, *, delay: float = 0.0, result: str = "ok", raise_exc: Exception | None = None):
        self._delay = delay
        self._result = result
        self._raise_exc = raise_exc
        self.calls: list[tuple[str, dict, float]] = []
        self.tools: list[DiscoveredMCPTool] = []

    async def call_tool(self, name, arguments, timeout_sec):
        self.calls.append((name, arguments, timeout_sec))
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
    result = manager.call_tool_sync("srv", "ping", {"a": 1}, timeout_sec=5)

    assert result == "pong"
    assert fake.calls == [("ping", {"a": 1}, 5)]


def test_call_tool_sync_unknown_server(background_loop):
    manager = MCPManager(background_loop)
    result = manager.call_tool_sync("missing", "ping", {}, timeout_sec=5)
    assert "unknown MCP server" in result


def test_call_tool_sync_times_out_and_cancels(background_loop):
    # call_tool_sync gives the outer, cross-thread wait a grace margin over
    # timeout_sec so a well-behaved connection's own read-timeout error wins;
    # this fake ignores timeout_sec entirely, so it must out-hang that margin
    # for the outer safety-net timeout to be the one that fires.
    manager = MCPManager(background_loop)
    fake = _FakeConnection(delay=3.0)
    manager._connections["srv"] = fake

    result = manager.call_tool_sync("srv", "slow", {}, timeout_sec=0.1)

    assert result == "⚠️ MCP_TOOL_ERROR (slow): timed out after 0.1s"


def test_call_tool_sync_propagates_errors(background_loop):
    manager = MCPManager(background_loop)
    fake = _FakeConnection(raise_exc=RuntimeError("boom"))
    manager._connections["srv"] = fake

    result = manager.call_tool_sync("srv", "bad", {}, timeout_sec=5)
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
    import json
    from concurrent.futures import ThreadPoolExecutor

    from new_core.llm.types import ToolCall, ToolCallFunction
    from new_core.tools.context import ToolContext
    from new_core.tools.providers.mcp import McpToolProvider
    from new_core.tools.schema import ToolSchema
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from ouroboros.policy import safety
    from tests._shared import collecting_event_sink, make_catalog

    # The round runs the dispatch gates too; an unconfigured safety backend would prepend a
    # warning to every result and this test is about the loop hand-off, not the gates.
    monkeypatch.setattr(safety, "check_safety", lambda *a, **kw: (True, ""))

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

    catalog = make_catalog([McpToolProvider(manager=manager, timeout_reader=lambda: 5)])
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)

    calls = [ToolCall(id="c0", function=ToolCallFunction(name="mcp__srv__ping", arguments=json.dumps({"a": 1})))]

    executor = build_tool_call_executor(catalog=catalog, drive_logs=tmp_path / "logs", events=collecting_event_sink())
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ouroboros-task") as pool:
        results = pool.submit(
            executor.execute,
            calls,
            ctx=ctx,
            task_id="task-mcp",
        ).result(timeout=10)

    assert [r["result"] for r in results] == ["pong"]
    assert fake.calls == [("ping", {"a": 1}, 5)]
