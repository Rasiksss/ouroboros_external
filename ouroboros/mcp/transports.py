"""Transport selection for MCP client connections."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from functools import partial
from types import TracebackType
from typing import Any

import httpx2
from mcp.client._transport import TransportStreams, WriteStream
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.message import ClientMessageMetadata, SessionMessage
from mcp.types import JSONRPCRequest

from ouroboros.app.config import get_mcp_stream_read_timeout_sec
from ouroboros.egress.auth.authenticators import static_headers
from ouroboros.egress.auth.client_guard import CredentialedClientGuard, RequestOrigin
from ouroboros.observability import aef_tracing
from ouroboros.storage.settings.schema import McpServerEntry

#: The SDK's own pair type, aliased rather than restated; pinned by a test so an SDK bump fails
#: there and not at import time during composition.
MCPStreams = TransportStreams

#: Set explicitly because httpx2 defaults every budget to 5s. `connection.py` sizes its start-up
#: budget from this one, so it is public.
TCP_CONNECT_TIMEOUT_SEC = 10.0
_HTTP_TIMEOUT_SEC = 30.0
_TOOL_CALL_METHOD = "tools/call"


class CallCredentials:
    def __init__(self) -> None:
        self._by_task: dict[asyncio.Task[Any], dict[str, str]] = {}

    @contextmanager
    def bound(self, headers: dict[str, str]) -> Iterator[None]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("call credentials can only be bound inside a task")
        self._by_task[task] = headers
        try:
            yield
        finally:
            del self._by_task[task]

    def of_current_task(self) -> dict[str, str]:
        task = asyncio.current_task()
        return self._by_task.get(task, {}) if task is not None else {}


class CredentialedWriteStream(WriteStream[SessionMessage]):
    def __init__(self, stream: WriteStream[SessionMessage], credentials: CallCredentials) -> None:
        self._stream = stream
        self._credentials = credentials

    async def send(self, item: SessionMessage, /) -> None:
        headers = self._credentials.of_current_task()
        message = item.message
        if headers and isinstance(message, JSONRPCRequest) and message.method == _TOOL_CALL_METHOD:
            metadata = item.metadata if isinstance(item.metadata, ClientMessageMetadata) else ClientMessageMetadata()
            item = replace(item, metadata=replace(metadata, headers={**(metadata.headers or {}), **headers}))
        await self._stream.send(item)

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def __aenter__(self) -> CredentialedWriteStream:
        await self._stream.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        return await self._stream.__aexit__(exc_type, exc_val, exc_tb)


def _guarded_http_client(
    origin: RequestOrigin,
    forwarded_headers: frozenset[str],
    static_header_names: frozenset[str],
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
) -> httpx2.AsyncClient:
    client = _http_client(headers=headers, timeout=timeout, auth=auth)
    if forwarded_headers:
        CredentialedClientGuard.keep_no_cookies(client)
    stripped = forwarded_headers | static_header_names
    if stripped:
        CredentialedClientGuard.strip_off_origin(client, origin, stripped)
    return client


def _http_timeout() -> httpx2.Timeout:
    return httpx2.Timeout(
        connect=TCP_CONNECT_TIMEOUT_SEC,
        read=get_mcp_stream_read_timeout_sec(),
        write=_HTTP_TIMEOUT_SEC,
        pool=_HTTP_TIMEOUT_SEC,
    )


def _narrowed_timeout(caller: httpx2.Timeout | None) -> httpx2.Timeout:
    """The caller's budgets, with `connect` held at or under this package's.

    `sse_client` spreads a single scalar over connect, write and pool, so without this an SSE
    connection would open on a different connect budget than a streamable one — and that budget is
    what `connection.py` derives its start-up budget from.
    """
    mine = _http_timeout()
    if caller is None:
        return mine
    return httpx2.Timeout(
        connect=mine.connect if caller.connect is None else min(caller.connect, TCP_CONNECT_TIMEOUT_SEC),
        read=mine.read if caller.read is None else caller.read,
        write=mine.write if caller.write is None else caller.write,
        pool=mine.pool if caller.pool is None else caller.pool,
    )


def _http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
) -> httpx2.AsyncClient:
    """An `McpHttpClientFactory`: the SDK's client plus the outbound trace headers.

    The SDK's factory supplies `follow_redirects` and its own hooks; the protocol exposes no
    `event_hooks` parameter, so appending to the returned client is the only seam.
    """
    client = create_mcp_http_client(headers=headers, timeout=_narrowed_timeout(timeout), auth=auth)
    client.event_hooks["request"] = [
        *client.event_hooks.get("request", []),
        aef_tracing.add_outbound_headers_on_request,
    ]
    return client


@asynccontextmanager
async def open_transport(entry: McpServerEntry, forwarded_headers: frozenset[str]) -> AsyncIterator[MCPStreams]:
    if entry.transport not in ("sse", "streamable_http"):
        raise ValueError(f"Unsupported MCP transport: {entry.transport!r}")
    client_headers = static_headers(entry.auth)
    guarded_http_client = partial(
        _guarded_http_client, RequestOrigin.of(httpx2.URL(entry.url)), forwarded_headers, frozenset(client_headers)
    )
    if entry.transport == "sse":
        async with sse_client(
            entry.url,
            headers=client_headers,
            timeout=_HTTP_TIMEOUT_SEC,
            sse_read_timeout=get_mcp_stream_read_timeout_sec(),
            httpx_client_factory=guarded_http_client,
        ) as streams:
            yield streams[0], streams[1]
        return

    async with (
        guarded_http_client(headers=client_headers, timeout=_http_timeout()) as http_client,
        streamable_http_client(entry.url, http_client=http_client) as streams,
    ):
        yield streams[0], streams[1]


__all__ = ["TCP_CONNECT_TIMEOUT_SEC", "CallCredentials", "CredentialedWriteStream", "open_transport"]
