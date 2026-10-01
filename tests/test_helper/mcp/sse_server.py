"""Local MCP test server — SSE transport, with header-based auth.

Run standalone:

    .venv/bin/python test_helper/mcp/sse_server.py

Listens on http://127.0.0.1:8931/sse
Requires header: X-API-Key: local-sse-secret
"""

from __future__ import annotations

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

AUTH_HEADER = "X-API-Key"
AUTH_TOKEN = "local-sse-secret"

mcp = FastMCP("local-sse", host="127.0.0.1", port=8931)


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
    app.add_middleware(_AuthMiddleware)
    uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port, log_level=mcp.settings.log_level.lower())
