"""A single MCP server connection: transport + session + tool discovery/calls.

anyio requires a ClientSession's cancel scope to be exited by the same Task that entered it, so
every session is opened, held, closed and re-opened by one background Task (`_supervise`);
`connect()` and `aclose()` only signal it. Calling list_tools/call_tool from other tasks is fine.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from typing import Any

import anyio
from mcp import ClientSession, types as mcp_types
from mcp.client import IncomingMessage
from mcp.client._probe import negotiate_auto
from mcp.client._transport import ReadStream
from mcp.client.session import DISCOVER_TIMEOUT_SECONDS
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from pydantic import ValidationError

from infra.user_tokens.interface import UserTokenTargetKind
from new_core.identity import OwnerScope
from new_core.tools.schema import ToolSchema
from ouroboros.app.config import get_mcp_request_timeout_sec
from ouroboros.egress.auth.authenticators import authenticator_for
from ouroboros.egress.auth.interfaces import EgressAuthDependencies, EgressAuthRefused, IEgressAuthenticator
from ouroboros.mcp.transports import (
    TCP_CONNECT_TIMEOUT_SEC,
    CallCredentials,
    CredentialedWriteStream,
    open_transport,
)
from ouroboros.observability import aef_tracing
from ouroboros.storage.settings.schema import McpServerEntry, describe_validation_error
from ouroboros.tools.naming import cap_tool_name, sanitize_tool_name

log = logging.getLogger(__name__)

_RECONNECT_MIN_DELAY_SEC = 1.0
_RECONNECT_MAX_DELAY_SEC = 60.0
#: Probe/handshake passes `negotiate_auto` can make — a `range(2)` literal in
#: `mcp/client/_probe.py`, pinned against the SDK's own source by `tests/ouroboros/mcp/test_mcp_client.py`.
_PROBE_PASSES = 2
#: Spent from the call's own timeout budget, never on top of it.
_RECONNECT_WAIT_SEC = 5.0
_TRACE_SOURCE = "mcp"
_HEALTHCHECK_INTERVAL_SEC = 30.0
_PING_TIMEOUT_SEC = 10.0
#: A gateway that swallows the transport's goodbye DELETE must not stall app shutdown.
_CLOSE_TIMEOUT_SEC = 15.0
_REQUEST_TIMEOUT_CODE = mcp_types.REQUEST_TIMEOUT
_SESSION_TERMINATED_MESSAGE = "Session terminated"
#: The messages the SDK pairs with CONNECTION_CLOSED when the session itself is gone; one request's
#: SSE stream ending is not one. Matched by message and not by code alone — see `_is_session_gone`.
_CONNECTION_CLOSED_MESSAGES = frozenset({"Connection closed"})
#: A union error carries one entry per arm, and this is logged once per unparseable frame.
_VALIDATION_DETAIL_LIMIT = 5


def _connect_budget_sec() -> float:
    """What `connect()` may spend before the first tool listing has returned.

    Computed from the spends it has to cover, not chosen: a `connect()` that times out is never
    retried — `MCPManager.initialize` skips the server for the life of the process — so a literal
    would quietly stop covering them when the request budget or the SDK's probe timeout moves.
    """
    request = get_mcp_request_timeout_sec()
    negotiation = _PROBE_PASSES * (DISCOVER_TIMEOUT_SECONDS + request)
    first_listing = request
    return TCP_CONNECT_TIMEOUT_SEC + negotiation + first_listing


class _ReadStreamEndWatcher(ReadStream[SessionMessage | Exception]):
    """The transport's read stream, wrapped so that the connection is told when it ends.

    A fault *on* the stream arrives as an item and reaches `message_handler`; a stream that simply
    **ends** carries nothing — the SDK's dispatcher leaves its `async for` without raising out of
    the session, so without this a tool call would be the first thing to notice. That is what a
    peer closing an idle connection looks like, and on `sse` the `GET` stream is held for the
    session's life. `last_context` is forwarded because the dispatcher duck-types it off the stream
    to run *inbound* messages under the context their sender queued them in.
    """

    def __init__(self, stream: Any, on_end: Callable[[], None]) -> None:
        self._stream = stream
        self._on_end = on_end

    @property
    def last_context(self) -> Any:
        return getattr(self._stream, "last_context", None)

    def __aiter__(self) -> _ReadStreamEndWatcher:
        return self

    async def __anext__(self) -> Any:
        try:
            return await self._stream.__anext__()
        except (StopAsyncIteration, anyio.ClosedResourceError):
            self._on_end()
            raise

    async def receive(self) -> Any:
        try:
            return await self._stream.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            self._on_end()
            raise

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def __aenter__(self) -> _ReadStreamEndWatcher:
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, *exc_info: Any) -> Any:
        return await self._stream.__aexit__(*exc_info)


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
    auth_dependencies: EgressAuthDependencies
    tools: list[DiscoveredMCPTool] = field(default_factory=list, init=False)
    _session: ClientSession | None = field(default=None, init=False)
    _session_task: asyncio.Task | None = field(default=None, init=False)
    #: Done for good; no session may be re-opened.
    _close_requested: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    #: End the current serving cycle. Cleared before each new one.
    _stop_serving: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _connected: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _session_retired: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _startup_error: Exception | None = field(default=None, init=False)
    _inflight: int = field(default=0, init=False)
    #: Loop time this cycle began serving, or `None` if it never did.
    _session_live_since: float | None = field(default=None, init=False)
    _logged_protocol_version: str | None = field(default=None, init=False)
    _tools_listed: bool = field(default=False, init=False)
    _call_credentials: CallCredentials = field(default_factory=CallCredentials, init=False)
    _authenticator: IEgressAuthenticator = field(init=False)
    _credentialed: bool = field(init=False)

    def __post_init__(self) -> None:
        self._authenticator = authenticator_for(self.entry, UserTokenTargetKind.MCP_SERVER, self.auth_dependencies)
        self._credentialed = bool(self._authenticator.forwarded_headers)

    async def connect(self) -> None:
        ready = asyncio.Event()
        self._session_task = asyncio.create_task(self._supervise(ready))

        try:
            with anyio.fail_after(_connect_budget_sec()):
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
                self._session_live_since = None
                try:
                    await self._serve_session(ready if not ever_live else None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    cause = _root_cause(exc)
                    if not ever_live and self._session_live_since is None:
                        # Nothing here ever worked: connect() re-raises and the manager drops
                        # the server, instead of retrying a misconfigured one forever.
                        self._startup_error = cause
                        log.warning("MCP server %s failed to connect: %r", self.entry.id, cause, exc_info=exc)
                        return
                    log.warning("MCP session for %s dropped: %r", self.entry.id, cause, exc_info=exc)
                served_for = self._served_for()
                if served_for is not None:
                    ever_live = True
                    if served_for >= delay:
                        delay = _RECONNECT_MIN_DELAY_SEC
                if await _wait_for(self._close_requested, delay):
                    return
                log.info("Reconnecting to MCP server %s", self.entry.id)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY_SEC)
        finally:
            ready.set()
            self._retire_session()

    def _served_for(self) -> float | None:
        """How long the cycle that just ended served, or `None` if it never did.

        The supervisor compares it against the pause it is about to take, never against an absolute
        threshold: the detectors can retire a session milliseconds after it started serving, so a
        ladder reset spent on "this cycle served" hands a flapping peer an unbounded
        rebuild-negotiate-relist loop at the bottom rung.
        """
        since = self._session_live_since
        return None if since is None else asyncio.get_running_loop().time() - since

    async def _serve_session(self, ready: asyncio.Event | None) -> None:
        """One session, from transport up to teardown. Raises when it drops.

        The session is an operation of its own: the probe, the handshake, the tool-list refresh and
        every idle keepalive have no caller. Its identifier is minted rather than inherited — a
        reconnect happening during a task would otherwise stamp that task's identifier on the
        session for the rest of the process — and the operation that asked for the connection is
        written down beside it, which only the first session has.
        """
        origin = aef_tracing.current_trace_id() if ready is not None else ""
        with aef_tracing.trace_scope(aef_tracing.accept_trace_id("", source=_TRACE_SOURCE)):
            log.info(
                "Opening an MCP session for %s%s",
                self.entry.id,
                f" source_trace_id={origin}" if origin else "",
            )
            await self._serve_scoped_session(ready)

    async def _serve_scoped_session(self, ready: asyncio.Event | None) -> None:
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(
                open_transport(self.entry, self._authenticator.forwarded_headers)
            )
            if self._credentialed:
                write = CredentialedWriteStream(write, self._call_credentials)
            session = await stack.enter_async_context(
                ClientSession(
                    _ReadStreamEndWatcher(read, self._drop_session),
                    write,
                    read_timeout_seconds=get_mcp_request_timeout_sec(),
                    message_handler=self._on_incoming_message,
                )
            )
            await negotiate_auto(session)
            self._log_negotiated_era(session)
            self._session_retired.clear()
            self._session = session
            try:
                await self._refresh_tools(session)
                self._session_live_since = asyncio.get_running_loop().time()
                self._connected.set()
                if ready is not None:
                    ready.set()
                await self._hold_session(session)
            finally:
                self._retire_session()

    def _log_negotiated_era(self, session: ClientSession) -> None:
        # `negotiate_auto` returns only once the version is set, so `None` here means "nothing
        # reported yet" and never a version that was — no second flag to keep in step.
        version = session.protocol_version
        if version == self._logged_protocol_version:
            log.debug("MCP server %s reconnected on protocol %s", self.entry.id, version)
            return
        log.info("MCP server %s speaks protocol %s", self.entry.id, version)
        self._logged_protocol_version = version

    async def _hold_session(self, session: ClientSession) -> None:
        if session.discover_result is not None:
            # `ping` left the protocol on 2026-07-28: an idle modern session sends nothing, and
            # there is no method left to reinstate here. An intermediary that closes the idle `sse`
            # stream is caught by `_ReadStreamEndWatcher` and costs a reconnect, not a wedged session.
            await self._stop_serving.wait()
            return

        while not await _wait_for(self._stop_serving, _HEALTHCHECK_INTERVAL_SEC):
            # Traffic of its own is proof enough, and a server busy with a long call owes no pong.
            if self._inflight == 0:
                await self._ping(session)

    async def _on_incoming_message(self, message: IncomingMessage) -> None:
        """End the serving cycle on a transport fault the SDK reports as data instead of raising.

        One of the two ways a session dies with nobody watching; `_ReadStreamEndWatcher` covers the
        other. Neither is gated on the protocol era, because the survival machinery is not.
        """
        if not isinstance(message, Exception):
            return
        if isinstance(message, ValidationError):
            # One unparseable frame, not a transport fault: the session is alive and a message was
            # lost. Described rather than rendered — `str(ValidationError)` embeds the frame, which
            # is a server's output and may carry credentials.
            log.warning(
                "MCP server %s sent a frame this client could not parse (%d validation errors: %s); "
                "the frame itself is withheld",
                self.entry.id,
                message.error_count(),
                describe_validation_error(message, limit=_VALIDATION_DETAIL_LIMIT),
            )
            return
        # `%r`: these are mostly timeouts, whose `str()` is the empty string.
        log.warning("MCP session for %s failed at the transport: %r", self.entry.id, message, exc_info=message)
        self._drop_session()

    def _drop_session(self) -> None:
        """Drop the live session and end the serving cycle, which alone can replace it.

        Both detectors reach this directly: the SDK joins the session's task group in
        `ClientSession.__aexit__`, so neither can outlive the cycle that installed it and fire
        against the session that replaced it.

        All three signals move together: until `_session` is cleared every further call is
        dispatched into a session that is already gone and waits out its own timeout there, and
        `_connected` left set makes `_await_session` hand that session straight back.
        """
        self._retire_session()
        self._stop_serving.set()

    def _retire_session(self) -> None:
        self._session = None
        self._connected.clear()
        self._session_retired.set()

    async def _ping(self, session: ClientSession) -> None:
        """Raise unless the channel is still there. A server's error reply counts as alive."""
        timed_out: Exception
        try:
            with anyio.fail_after(_PING_TIMEOUT_SEC):
                await session.send_ping()
            return
        except MCPError as exc:
            if _is_session_gone(exc.error):
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

    async def _refresh_tools(self, session: ClientSession) -> None:
        """List `session`'s tools, following pagination.

        The session is a parameter and not `self._session`, which either detector can null between
        two pages: re-reading the attribute would turn a drop mid-listing into an `AttributeError`
        instead of the transport error it is. `call_tool` captures it into a local for the same
        reason.
        """
        allowed = set(self.entry.allowed_tools) if self.entry.allowed_tools else None

        discovered: list[DiscoveredMCPTool] = []
        seen_names: set[str] = set()
        listed = 0
        cursor: str | None = None
        while True:
            result = await session.list_tools(params=mcp_types.PaginatedRequestParams(cursor=cursor))
            listed += len(result.tools)
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
                    parameters=tool.input_schema or {"type": "object", "properties": {}},
                )
                discovered.append(
                    DiscoveredMCPTool(
                        server_id=self.entry.id,
                        original_name=tool.name,
                        full_name=full_name,
                        schema=schema,
                    )
                )
            cursor = result.next_cursor
            if not cursor:
                break

        previous = {tool.full_name for tool in self.tools}
        self.tools = discovered
        current = seen_names
        if not self._tools_listed:
            # A flag and not `previous` being empty: a server that publishes nothing has an empty
            # one on every reconnect. Both counts, because `allowed_tools` sits between them.
            log.info(
                "MCP server %s listed %d tools, %d after this entry's allow-list: %s",
                self.entry.id,
                listed,
                len(current),
                sorted(current),
            )
            self._tools_listed = True
        elif previous != current:
            log.warning(
                "MCP server %s changed its tool set across a reconnect (gained %s, lost %s); "
                "dispatch still goes by the set discovered at startup",
                self.entry.id,
                sorted(current - previous),
                sorted(previous - current),
            )

    async def call_tool(
        self,
        scope: OwnerScope,
        name: str,
        arguments: dict[str, Any] | None,
        timeout_sec: float,
    ) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec

        session = await (self._await_listed_session if self._credentialed else self._await_session)(timeout_sec)
        if session is None or deadline - loop.time() <= 0:
            return format_mcp_tool_error(
                name, f"server {self.entry.id!r} is not connected; a reconnect is in progress, retry shortly"
            )
        if self._credentialed and name not in {tool.original_name for tool in self.tools}:
            return format_mcp_tool_error(name, f"server {self.entry.id!r} did not list this tool")

        try:
            credentials = await self._authenticator.request_headers(scope)
        except EgressAuthRefused as refusal:
            log.warning("MCP tools/call to %s/%s was not authorized: %s", self.entry.id, name, refusal.reason)
            return format_mcp_tool_error(name, f"server {self.entry.id!r} cannot be authorized: {refusal.reason}")
        remaining = deadline - loop.time()
        if remaining <= 0:
            return format_mcp_tool_error(name, f"timed out after {timeout_sec}s")

        self._inflight += 1
        try:
            with self._call_credentials.bound(credentials):
                result = await session.call_tool(
                    name,
                    arguments or {},
                    read_timeout_seconds=remaining,
                )
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            log.warning("MCP tools/call failed for %s/%s on a dead session: %s", self.entry.id, name, exc)
            self._note_dead_session(session)
            return format_mcp_tool_error(name, exc)
        except MCPError as exc:
            log.warning("MCP tools/call failed for %s/%s: %s", self.entry.id, name, exc)
            if _is_session_gone(exc.error):
                self._note_dead_session(session)
            return format_mcp_tool_error(name, exc)
        except Exception as exc:
            log.warning("MCP tools/call failed for %s/%s: %s", self.entry.id, name, exc)
            return format_mcp_tool_error(name, exc)
        finally:
            self._inflight -= 1

        text = _format_call_tool_result(result)
        if result.is_error:
            return format_mcp_tool_error(name, text)
        return text

    def _note_dead_session(self, session: ClientSession) -> None:
        """Retire a dead session and wake the supervisor, which alone can replace it.

        The identity check matters for CONNECTION_CLOSED, which reaches a call that was already
        waiting — possibly after the supervisor has moved on.
        """
        if self._session is not session:
            return
        self._drop_session()

    async def _await_session(self, timeout_sec: float) -> ClientSession | None:
        """The live session, waiting out a reconnect rather than failing the call outright."""
        session = self._session
        if session is not None:
            return session
        await _wait_for(self._connected, min(timeout_sec, _RECONNECT_WAIT_SEC))
        return self._session

    async def _await_listed_session(self, timeout_sec: float) -> ClientSession | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        if self._session is None:
            await _wait_for(self._connected, min(timeout_sec, _RECONNECT_WAIT_SEC))
        if not self._connected.is_set() and self._session is not None:
            await self._wait_for_any((self._connected, self._session_retired), max(0.0, deadline - loop.time()))
        return self._session if self._connected.is_set() else None

    @classmethod
    async def _wait_for_any(cls, events: tuple[asyncio.Event, ...], timeout_sec: float) -> None:
        with anyio.move_on_after(timeout_sec):
            async with anyio.create_task_group() as group:
                for event in events:
                    group.start_soon(cls._cancel_when_set, event, group.cancel_scope)

    @staticmethod
    async def _cancel_when_set(event: asyncio.Event, scope: anyio.CancelScope) -> None:
        await event.wait()
        scope.cancel()

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


def _is_session_gone(error: mcp_types.ErrorData) -> bool:
    """Whether this error is the client's own report that the channel is gone.

    Both arms match the message as well as the code, because a *server* may legitimately answer
    with either code — and writing off a live session costs a reconnect on every call.
    """
    if error.code == mcp_types.CONNECTION_CLOSED:
        return error.message in _CONNECTION_CLOSED_MESSAGES
    return error.code == mcp_types.INVALID_REQUEST and error.message == _SESSION_TERMINATED_MESSAGE


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
