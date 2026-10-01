"""MCP server list for this image — the security-owned source of the list.

Editing this file is the only way to change which MCP servers the process talks to, so a change
goes through code review, CI and image signing. The list is built once at import time and is fixed
for the process lifetime; secrets are read here, never written here.
"""

import os

from ouroboros.storage.settings.schema import McpServerEntry
from ouroboros.storage.settings.store import load_settings


def _token(name: str) -> str:
    """Secret for `name` from the environment — replace the body with the team's secret engine."""
    return os.getenv(name, "")


_polygon = load_settings().OUROBOROS_POLIGON

mcp_servers: list[McpServerEntry]

if _polygon == "DEV":
    # Legacy path: the list still comes from settings, as before this file existed.
    mcp_servers = list(load_settings().MCP_SERVERS)
elif _polygon == "EXAMPLE":
    mcp_servers = [
        McpServerEntry(
            id="example-mcp",
            name="Example MCP",
            transport="streamable_http",
            url="https://mcp.example.internal/mcp",
            auth_header="X-API-Key",
            auth_token=_token("MCP_EXAMPLE_TOKEN"),
            allowed_tools=["search"],
            retrieval_aliases=["example"],
        ),
    ]
else:
    mcp_servers = []
