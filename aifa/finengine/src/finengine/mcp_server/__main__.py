"""Run the finengine MCP server: `python -m finengine.mcp_server`.

Serves the streamable-http app on FINENGINE_MCP_HOST:FINENGINE_MCP_PORT
(default 0.0.0.0:8848, mount path `/mcp`). The OpenCLAW gateway registers it via
`mcp.servers.finengine = { url: "http://<host>:8848/mcp", transport: "streamable-http" }`.
"""
from __future__ import annotations

import os

import uvicorn

from finengine.mcp_server import app


def main() -> None:
    host = os.environ.get("FINENGINE_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("FINENGINE_MCP_PORT", "8848"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
