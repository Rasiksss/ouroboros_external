"""Local MCP test server — SSE transport, with header-based auth.

Run standalone:

    .venv/bin/python test_helper/mcp/sse_server.py

Listens on http://127.0.0.1:8931/sse
Requires header: X-API-Key: $MCP_TEST_AUTH_TOKEN (local-sse-secret when unset, none when empty)
"""

from __future__ import annotations

import os

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

AUTH_HEADER = "X-API-Key"
AUTH_TOKEN = os.environ.get("MCP_TEST_AUTH_TOKEN", "local-sse-secret")

#: 2.x drops host/port from the server's settings; the app builder and uvicorn take them.
HOST = "127.0.0.1"
PORT = 8931

mcp = MCPServer("local-sse")


@mcp.tool()
def ping() -> str:
    """Health check — returns a fixed pong reply."""
    return "pong from local-sse"


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.headers.get(AUTH_HEADER) != AUTH_TOKEN:
            return PlainTextResponse("Unauthorized", status_code=401)
        return await call_next(request)


if __name__ == "__main__":
    app = mcp.sse_app()
    if AUTH_TOKEN:
        app.add_middleware(_AuthMiddleware)
    uvicorn.run(app, host=HOST, port=PORT, log_level=mcp.settings.log_level.lower())
