"""Transport selection for MCP client connections."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.message import SessionMessage

from ouroboros.storage.settings.schema import McpServerEntry

MCPStreams = tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception],
    MemoryObjectSendStream[SessionMessage],
]

#: Set explicitly because httpx defaults to 5s for everything — under the MCP-level tool
#: timeout — and a read timeout there does not fail one call, it kills the whole session.
_CONNECT_TIMEOUT_SEC = 10.0
_HTTP_TIMEOUT_SEC = 30.0
_STREAM_READ_TIMEOUT_SEC = 300.0


def _auth_headers(entry: McpServerEntry) -> dict[str, str]:
    if entry.auth_header and entry.auth_token:
        return {entry.auth_header: entry.auth_token}
    return {}


def _merged_env(env: dict[str, str]) -> dict[str, str]:
    merged = get_default_environment()
    merged.update(env)
    return merged


def _http_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=_CONNECT_TIMEOUT_SEC,
        read=_STREAM_READ_TIMEOUT_SEC,
        write=_HTTP_TIMEOUT_SEC,
        pool=_HTTP_TIMEOUT_SEC,
    )


@asynccontextmanager
async def open_transport(entry: McpServerEntry) -> AsyncIterator[MCPStreams]:
    """Open the read/write stream pair for a server's configured transport."""
    if entry.transport == "stdio":
        params = StdioServerParameters(
            command=entry.command,
            args=entry.args,
            env=_merged_env(entry.env),
        )
        async with stdio_client(params) as streams:
            yield streams[0], streams[1]
        return

    if entry.transport == "sse":
        async with sse_client(
            entry.url,
            headers=_auth_headers(entry),
            timeout=_HTTP_TIMEOUT_SEC,
            sse_read_timeout=_STREAM_READ_TIMEOUT_SEC,
        ) as streams:
            yield streams[0], streams[1]
        return

    if entry.transport == "streamable_http":
        # follow_redirects: as in the SDK's own factory — a 307 from a gateway is otherwise fatal.
        async with (
            httpx.AsyncClient(
                headers=_auth_headers(entry),
                timeout=_http_timeout(),
                follow_redirects=True,
            ) as http_client,
            streamable_http_client(entry.url, http_client=http_client) as streams,
        ):
            yield streams[0], streams[1]
        return

    raise ValueError(f"Unsupported MCP transport: {entry.transport!r}")


__all__ = ["open_transport"]
