"""Local MCP test server — streamable HTTP transport, with header-based auth.

Run standalone:

    .venv/bin/python test_helper/mcp/streamable_http_server.py

Listens on http://127.0.0.1:8932/mcp
Requires header: X-API-Key: $MCP_TEST_AUTH_TOKEN (local-http-secret when unset, none when empty)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

AUTH_HEADER = "X-API-Key"
AUTH_TOKEN = os.environ.get("MCP_TEST_AUTH_TOKEN", "local-http-secret")

#: Default delay for `slow_echo`, so the tool can be made slower than MCP_TOOL_TIMEOUT_SEC
#: without editing the call site.
SLOW_DELAY_SEC = float(os.environ.get("MCP_SLOW_DELAY_SEC", "90"))

#: 2.x drops host/port from the server's settings; the app builder and uvicorn take them.
HOST = os.environ.get("MCP_TEST_BIND_HOST", "127.0.0.1")
PORT = 8932
SERVER_NAME = os.environ.get("MCP_TEST_SERVER_NAME", "local-http")

mcp = MCPServer(SERVER_NAME)


@mcp.tool()
def ping() -> str:
    """Health check — returns a fixed pong reply."""
    return f"pong from {SERVER_NAME}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
async def slow_echo(text: str, seconds: float = 0.0) -> str:
    """Echo `text` after a delay — a healthy tool that simply answers late."""
    delay = float(seconds) if seconds > 0 else SLOW_DELAY_SEC
    started = time.monotonic()
    # stderr: uvicorn owns stdout here, and the timing is the point of the fixture.
    print(f"[{SERVER_NAME}] slow_echo start delay={delay}s", file=sys.stderr, flush=True)
    await asyncio.sleep(delay)
    print(f"[{SERVER_NAME}] slow_echo done after {time.monotonic() - started:.1f}s", file=sys.stderr, flush=True)
    return f"echo: {text}"


class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.headers.get(AUTH_HEADER) != AUTH_TOKEN:
            return PlainTextResponse("Unauthorized", status_code=401)
        return await call_next(request)


if __name__ == "__main__":
    app = mcp.streamable_http_app(host=HOST)
    if AUTH_TOKEN:
        app.add_middleware(_AuthMiddleware)
    uvicorn.run(app, host=HOST, port=PORT, log_level=mcp.settings.log_level.lower())
