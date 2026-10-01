"""A single MCP server connection: transport + session + tool discovery/calls.

anyio requires a ClientSession's cancel scope to be exited by the same Task that entered it,
and connect()/aclose() run in whatever Task calls them — so every session is opened, held and
closed by one dedicated background Task (`_supervise`); those two only signal it. Calling
list_tools/call_tool from other tasks is fine, only the context manager is task-affine.

That task also re-opens the session after a drop: one exception in the HTTP layer kills a
streamable-HTTP session without touching the server, so a process that cannot re-open one
serves "not connected" until it is restarted.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import anyio
from mcp import ClientSession, types as mcp_types
from mcp.shared.exceptions import McpError

from new_core.tools.schema import ToolSchema
from ouroboros.mcp.transports import open_transport
from ouroboros.storage.settings.schema import McpServerEntry
from ouroboros.tools.naming import cap_tool_name, sanitize_tool_name

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SEC = 30.0
_SESSION_READ_TIMEOUT_SEC = 30.0
_RECONNECT_MIN_DELAY_SEC = 1.0
_RECONNECT_MAX_DELAY_SEC = 60.0
#: Spent from the call's own timeout budget, never on top of it.
_RECONNECT_WAIT_SEC = 5.0
#: The idle ping doubles as what keeps a gateway from timing the connection out.
_HEALTHCHECK_INTERVAL_SEC = 30.0
_PING_TIMEOUT_SEC = 10.0
#: A gateway that swallows the transport's goodbye DELETE must not stall app shutdown.
_CLOSE_TIMEOUT_SEC = 15.0
_REQUEST_TIMEOUT_CODE = 408
#: Errors the client synthesized for a session it wrote off, not replies a server sent: 32600
#: is a 404 on the POST (the server forgot our session id), CONNECTION_CLOSED is the receive
#: loop failing pending requests. Both leave the streams open, so nothing else notices.
_SESSION_GONE_CODES = frozenset({32600, mcp_types.CONNECTION_CLOSED})


def _build_mcp_tool_name(server_id: str, tool_name: str) -> str:
    return cap_tool_name(f"mcp__{sanitize_tool_name(server_id)}__{sanitize_tool_name(tool_name)}")


def format_mcp_tool_error(name: str, detail: object) -> str:
    return f"⚠️ MCP_TOOL_ERROR ({name}): {detail}"


@dataclass
class DiscoveredMCPTool:
    server_id: str
    original_name: str
    full_name: str
    schema: ToolSchema


@dataclass
class MCPServerConnection:
    """Owns one MCP server's transport, session, and cached tool list."""

    entry: McpServerEntry
    tools: list[DiscoveredMCPTool] = field(default_factory=list, init=False)
    _session: ClientSession | None = field(default=None, init=False)
    _session_task: asyncio.Task | None = field(default=None, init=False)
    #: Done for good; no session may be re-opened.
    _close_requested: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    #: End the current serving cycle. Cleared before each new one.
    _stop_serving: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _connected: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _startup_error: Exception | None = field(default=None, init=False)
    _inflight: int = field(default=0, init=False)
    _session_served: bool = field(default=False, init=False)

    async def connect(self) -> None:
        ready = asyncio.Event()
        self._session_task = asyncio.create_task(self._supervise(ready))

        try:
            with anyio.fail_after(_CONNECT_TIMEOUT_SEC):
                await ready.wait()
        except TimeoutError:
            self._session_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._session_task
            raise

        if self._startup_error is not None:
            # The caller drops this connection, so the supervisor must not outlive it.
            await self.aclose()
            raise self._startup_error

    async def _supervise(self, ready: asyncio.Event) -> None:
        """Open sessions one at a time for this connection's lifetime, all in this Task."""
        delay = _RECONNECT_MIN_DELAY_SEC
        ever_live = False
        try:
            while not self._close_requested.is_set():
                self._stop_serving.clear()
                self._session_served = False
                try:
                    await self._serve_session(ready if not ever_live else None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    cause = _root_cause(exc)
                    if not (ever_live or self._session_served):
                        # Nothing here ever worked: connect() re-raises and the manager drops
                        # the server, instead of retrying a misconfigured one forever.
                        self._startup_error = cause
                        log.warning("MCP server %s failed to connect: %s", self.entry.id, cause, exc_info=exc)
                        return
                    log.warning("MCP session for %s dropped: %s", self.entry.id, cause, exc_info=exc)
                if self._session_served:
                    ever_live = True
                    delay = _RECONNECT_MIN_DELAY_SEC
                if await _wait_for(self._close_requested, delay):
                    return
                log.info("Reconnecting to MCP server %s", self.entry.id)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY_SEC)
        finally:
            ready.set()
            self._connected.clear()
            self._session = None

    async def _serve_session(self, ready: asyncio.Event | None) -> None:
        """One session, from transport up to teardown. Raises when it drops."""
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(open_transport(self.entry))
            session = await stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=_SESSION_READ_TIMEOUT_SEC))
            )
            await session.initialize()
            self._session = session
            try:
                await self._refresh_tools()
                self._session_served = True
                self._connected.set()
                if ready is not None:
                    ready.set()
                await self._hold_session(session)
            finally:
                self._connected.clear()
                self._session = None

    async def _hold_session(self, session: ClientSession) -> None:
        """Hold a live session until it is stopped, pinging it while it sits idle."""
        while not await _wait_for(self._stop_serving, _HEALTHCHECK_INTERVAL_SEC):
            # Traffic of its own is proof enough, and a server busy with a long call owes no pong.
            if self._inflight == 0:
                await self._ping(session)

    async def _ping(self, session: ClientSession) -> None:
        """Raise unless the channel is still there. A server's error reply counts as alive."""
        timed_out: Exception
        try:
            with anyio.fail_after(_PING_TIMEOUT_SEC):
                await session.send_ping()
            return
        except McpError as exc:
            if exc.error.code in _SESSION_GONE_CODES:
                raise
            if exc.error.code != _REQUEST_TIMEOUT_CODE:
                log.debug("MCP server %s refused ping (%s); the session is still alive", self.entry.id, exc)
                return
            timed_out = exc
        except TimeoutError as exc:
            timed_out = exc

        if self._inflight:
            # A call arrived while the ping was out; dropping the session would take it down.
            log.debug("MCP server %s let an idle ping lapse while serving a call", self.entry.id)
            return
        raise timed_out

    async def _refresh_tools(self) -> None:
        assert self._session is not None
        allowed = set(self.entry.allowed_tools) if self.entry.allowed_tools else None

        discovered: list[DiscoveredMCPTool] = []
        seen_names: set[str] = set()
        cursor: str | None = None
        while True:
            result = await self._session.list_tools(params=mcp_types.PaginatedRequestParams(cursor=cursor))
            for tool in result.tools:
                if allowed is not None and tool.name not in allowed:
                    continue
                description = tool.description or ""
                full_name = _build_mcp_tool_name(self.entry.id, tool.name)
                if full_name in seen_names:
                    log.warning(
                        "Skipping MCP tool %s/%s: sanitized name %s collides with an earlier tool",
                        self.entry.id,
                        tool.name,
                        full_name,
                    )
                    continue
                seen_names.add(full_name)
                schema = ToolSchema(
                    name=full_name,
                    description=f"[MCP {self.entry.id}] {description}"
                    if description
                    else f"MCP tool from {self.entry.id}",
                    parameters=tool.inputSchema or {"type": "object", "properties": {}},
                )
                discovered.append(
                    DiscoveredMCPTool(
                        server_id=self.entry.id,
                        original_name=tool.name,
                        full_name=full_name,
                        schema=schema,
                    )
                )
            cursor = result.nextCursor
            if not cursor:
                break

        previous = {tool.full_name for tool in self.tools}
        self.tools = discovered
        current = seen_names
        if previous and previous != current:
            log.warning(
                "MCP server %s changed its tool set across a reconnect (gained %s, lost %s); "
                "dispatch still goes by the set discovered at startup",
                self.entry.id,
                sorted(current - previous),
                sorted(previous - current),
            )

    async def call_tool(self, name: str, arguments: dict[str, Any] | None, timeout_sec: float) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec

        session = await self._await_session(timeout_sec)
        remaining = deadline - loop.time()
        if session is None or remaining <= 0:
            return format_mcp_tool_error(
                name, f"server {self.entry.id!r} is not connected; a reconnect is in progress, retry shortly"
            )

        self._inflight += 1
        try:
            result = await session.call_tool(
                name,
                arguments or {},
                read_timeout_seconds=timedelta(seconds=remaining),
            )
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            log.warning("MCP tools/call failed for %s/%s on a dead session: %s", self.entry.id, name, exc)
            self._note_dead_session(session)
            return format_mcp_tool_error(name, exc)
        except McpError as exc:
            log.warning("MCP tools/call failed for %s/%s: %s", self.entry.id, name, exc)
            if exc.error.code in _SESSION_GONE_CODES:
                self._note_dead_session(session)
            return format_mcp_tool_error(name, exc)
        except Exception as exc:
            log.warning("MCP tools/call failed for %s/%s: %s", self.entry.id, name, exc)
            return format_mcp_tool_error(name, exc)
        finally:
            self._inflight -= 1

        text = _format_call_tool_result(result)
        if result.isError:
            return format_mcp_tool_error(name, text)
        return text

    def _note_dead_session(self, session: ClientSession) -> None:
        """Retire a dead session and wake the supervisor, which alone can replace it.

        The handle is dropped here rather than left to the supervisor because until it is,
        every further call is dispatched into the same dead session and hangs out its own
        timeout there. The identity check matters for CONNECTION_CLOSED, which reaches a call
        that was already waiting — possibly after the supervisor has moved on."""
        if self._session is not session:
            return
        self._session = None
        self._connected.clear()
        self._stop_serving.set()

    async def _await_session(self, timeout_sec: float) -> ClientSession | None:
        """The live session, waiting out a reconnect rather than failing the call outright."""
        session = self._session
        if session is not None:
            return session
        await _wait_for(self._connected, min(timeout_sec, _RECONNECT_WAIT_SEC))
        return self._session

    async def aclose(self) -> None:
        task = self._session_task
        if task is None:
            return
        self._close_requested.set()
        self._stop_serving.set()
        try:
            with anyio.fail_after(_CLOSE_TIMEOUT_SEC):
                await task
        except TimeoutError:
            log.warning(
                "MCP server %s did not close its session in %.0fs; cancelled it", self.entry.id, _CLOSE_TIMEOUT_SEC
            )
            with suppress(asyncio.CancelledError):
                await task
        except asyncio.CancelledError:
            # The supervisor's own cancellation is not this caller's to inherit.
            if not task.cancelled():
                raise


async def _wait_for(event: asyncio.Event, timeout_sec: float) -> bool:
    """True once `event` is set; False if `timeout_sec` ran out first."""
    with anyio.move_on_after(timeout_sec):
        await event.wait()
    return event.is_set()


def _root_cause(exc: Exception) -> Exception:
    """The one error worth reading, out of the ExceptionGroup an anyio task group hands up."""
    current = exc
    while True:
        nested = getattr(current, "exceptions", None)
        if nested is None or len(nested) != 1 or not isinstance(nested[0], Exception):
            return current
        current = nested[0]


def _format_call_tool_result(result: mcp_types.CallToolResult) -> str:
    texts: list[str] = []
    for block in result.content:
        if isinstance(block, mcp_types.TextContent):
            texts.append(block.text)
        else:
            texts.append(str(block))
    return "\n".join(texts)


__all__ = ["DiscoveredMCPTool", "MCPServerConnection"]
