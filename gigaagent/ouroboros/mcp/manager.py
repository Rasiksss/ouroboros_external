"""Process-wide MCP client manager — a lifespan-initialized singleton.

`mcp_manager` is a module-level instance created at import time. Its
`initialize()` is called once from the app's `lifespan()` startup hook (see
server.py), after which any other module imports the `mcp_manager` object
directly and calls its methods to reach the live connections. There is no
class-level singleton lookup here: before `initialize()` runs, `mcp_manager`
simply behaves as an empty manager (no connections, no discovered tools),
so callers never need to guard reads with an "is it initialized" check —
only `initialize()`/`shutdown()` themselves are guarded against being
called out of order.
"""

from __future__ import annotations

import asyncio
import logging
from asyncio import AbstractEventLoop
from typing import Any

from new_core.identity import OwnerScope
from new_core.tools.loop_bridge import BridgeTimeout, call_on_loop_sync
from ouroboros.egress.auth.interfaces import EgressAuthDependencies
from ouroboros.mcp.connection import DiscoveredMCPTool, MCPServerConnection, format_mcp_tool_error
from ouroboros.storage.settings.schema import McpServerEntry

log = logging.getLogger(__name__)


class MCPManager:
    """Owns connections to all configured MCP servers for the process lifetime."""

    def __init__(self, loop: AbstractEventLoop | None = None) -> None:
        self._loop = loop
        self._connections: dict[str, MCPServerConnection] = {}
        self._tool_index: dict[str, DiscoveredMCPTool] = {}
        self._initialized = False

    async def initialize(
        self,
        servers: list[McpServerEntry],
        loop: AbstractEventLoop,
        auth_dependencies: EgressAuthDependencies,
    ) -> None:
        if self._initialized:
            raise RuntimeError("MCPManager.initialize() was already called")

        self._loop = loop
        enabled = [entry for entry in servers if entry.enabled and entry.id.strip()]
        try:
            results = await asyncio.gather(
                *(self._connect_one(entry, auth_dependencies) for entry in enabled),
                return_exceptions=True,
            )
        except BaseException:
            await self.shutdown()
            raise
        for entry, result in zip(enabled, results, strict=True):
            if isinstance(result, Exception):
                log.warning("Skipping MCP server %s after connection failure: %s", entry.id, result)

        for connection in self._connections.values():
            for tool in connection.tools:
                existing = self._tool_index.get(tool.full_name)
                if existing is not None:
                    log.warning(
                        "MCP tool name collision: %s from server %s already maps to %s/%s; keeping the first",
                        tool.full_name,
                        tool.server_id,
                        existing.server_id,
                        existing.original_name,
                    )
                    continue
                self._tool_index[tool.full_name] = tool

        self._initialized = True

    async def _connect_one(self, entry: McpServerEntry, auth_dependencies: EgressAuthDependencies) -> None:
        connection = MCPServerConnection(entry, auth_dependencies)
        try:
            await connection.connect()
        except BaseException:
            await connection.aclose()
            raise
        self._connections[entry.id] = connection

    def is_available(self) -> bool:
        """True when initialized AND the live MCP_ENABLED setting still allows
        dispatch — an instant kill switch, distinct from the one-time lifecycle
        fact that `initialize()` has run. Connections stay open either way;
        this only gates whether they're offered/dispatchable right now."""
        if not self._initialized:
            return False
        from ouroboros.app.config import get_mcp_enabled

        return bool(get_mcp_enabled())

    def discovered_tools(self) -> list[DiscoveredMCPTool]:
        """All tools discovered across all connected servers."""
        return list(self._tool_index.values())

    def resolve(self, full_name: str) -> tuple[str, str] | None:
        """Return (server_id, original_name) for a known `mcp__{server}__{tool}` name."""
        tool = self._tool_index.get(full_name)
        if tool is None:
            return None
        return tool.server_id, tool.original_name

    def retrieval_aliases(self, full_name: str) -> tuple[str, ...]:
        """Return aliases configured for the discovered tool's owning MCP server."""
        tool = self._tool_index.get(full_name)
        if tool is None:
            return ()
        connection = self._connections.get(tool.server_id)
        if connection is None:
            return ()
        return tuple(connection.entry.retrieval_aliases)

    def tool_result_limit(self, full_name: str) -> int | None:
        """Return the configured result cap for a discovered MCP tool."""
        tool = self._tool_index.get(full_name)
        if tool is None:
            return None
        connection = self._connections.get(tool.server_id)
        if connection is None:
            return None
        return connection.entry.tool_result_limits.get(tool.original_name)

    def call_tool_sync(
        self,
        scope: OwnerScope,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_sec: float,
    ) -> str:
        """Bridge a sync tool-handler call (running on a worker thread) onto the
        main event loop where the MCP session actually lives."""
        connection = self._connections.get(server_id)
        if connection is None:
            return format_mcp_tool_error(tool_name, f"unknown MCP server {server_id!r}")
        if self._loop is None:
            return format_mcp_tool_error(tool_name, "MCP manager is not initialized")

        # connection.call_tool() enforces its own read_timeout_seconds == timeout_sec
        # and returns a clean, formatted result on that timeout; the bridge's grace margin
        # is what keeps that inner timeout the one that fires at the boundary.
        try:
            return call_on_loop_sync(
                self._loop,
                lambda: connection.call_tool(scope, tool_name, arguments, timeout_sec),
                timeout_sec=timeout_sec,
            )
        except BridgeTimeout:
            return format_mcp_tool_error(tool_name, f"timed out after {timeout_sec}s")
        except Exception as exc:
            return format_mcp_tool_error(tool_name, exc)

    async def shutdown(self) -> None:
        """Close all connections and reset state. Safe to call even if
        `initialize()` never ran — server.py can call it unconditionally
        during lifespan teardown."""
        await asyncio.gather(
            *(connection.aclose() for connection in self._connections.values()),
            return_exceptions=True,
        )
        self._connections.clear()
        self._tool_index.clear()
        self._initialized = False


mcp_manager = MCPManager()

__all__ = ["MCPManager", "mcp_manager"]
