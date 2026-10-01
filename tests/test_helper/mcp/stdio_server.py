"""Local MCP test server — stdio transport.

Run directly (the client spawns this as a subprocess, so it's normally
never launched by hand):

    .venv/bin/python test_helper/mcp/stdio_server.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from mcp.server.fastmcp import FastMCP

#: Default delay for `slow_echo`, so the tool can be made slower than MCP_TOOL_TIMEOUT_SEC
#: without editing the call site. See scripts/repro/README.md.
SLOW_DELAY_SEC = float(os.environ.get("MCP_SLOW_DELAY_SEC", "90"))

mcp = FastMCP("local-stdio")


@mcp.tool()
def ping() -> str:
    """Health check — returns a fixed pong reply."""
    return "pong from local-stdio"


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
async def slow_echo(text: str, seconds: float = 0.0) -> str:
    """Echo `text` after a delay — a healthy tool that simply answers late."""
    delay = float(seconds) if seconds > 0 else SLOW_DELAY_SEC
    started = time.monotonic()
    # stderr: stdout is the transport, and the timing is the point of the fixture.
    print(f"[local-stdio] slow_echo start delay={delay}s", file=sys.stderr, flush=True)
    await asyncio.sleep(delay)
    print(f"[local-stdio] slow_echo done after {time.monotonic() - started:.1f}s", file=sys.stderr, flush=True)
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
