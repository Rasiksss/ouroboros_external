"""MCP servers: tools discovered over configured connections."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from new_core.tools.interfaces import IToolProvider, ToolDescriptor
from new_core.tools.runtimes import THREAD_RUNTIME
from new_core.tools.schema import ToolSchema
from ouroboros.tools.catalog import RESULT_LIMIT_HARD_MAX, spec_for_tool

if TYPE_CHECKING:
    from new_core.tools.context import ToolContext

log = logging.getLogger(__name__)

DEFAULT_MCP_TOOL_TIMEOUT_SEC = 120


class McpToolProvider(IToolProvider):
    """Tools reached through the process-wide MCP manager.

    Asynchrony stays inside ``refresh()``: the manager's ``initialize()`` is a coroutine,
    and it is driven onto the constructor's loop so callers see a synchronous refresh like
    every other provider.
    """

    default_runtime = THREAD_RUNTIME

    def __init__(
        self,
        servers: Sequence[Any] = (),
        loop: asyncio.AbstractEventLoop | None = None,
        timeout_reader: Callable[[], float] | None = None,
        manager: Any | None = None,
    ) -> None:
        self._servers = list(servers)
        self._loop = loop
        self._timeout_reader = timeout_reader
        self._manager = manager

    def _mgr(self) -> Any:
        if self._manager is not None:
            return self._manager
        from ouroboros.mcp.manager import mcp_manager

        return mcp_manager

    def _timeout(self) -> float:
        """Read the tool timeout at call time — it is a live setting, not a build-time fact.

        Returned as the reader gives it (the config reader yields an ``int``), because the
        value reaches ``call_tool_sync`` and surfaces in MCP timeout messages.
        """
        if self._timeout_reader is not None:
            return self._timeout_reader()
        try:
            from ouroboros.app.config import get_mcp_tool_timeout_sec

            return get_mcp_tool_timeout_sec()
        except Exception:
            return DEFAULT_MCP_TOOL_TIMEOUT_SEC

    def refresh(self) -> None:
        manager = self._mgr()
        if getattr(manager, "_initialized", False) or not self._servers or self._loop is None:
            # Already connected (server lifespan owns the one-shot initialize), or nothing
            # to connect. Discovery is read live from the manager either way.
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                manager.initialize(self._servers, self._loop),
                self._loop,
            )
            future.result(timeout=self._timeout() + 2)
        except Exception:
            log.warning("MCP provider refresh failed; continuing without MCP tools", exc_info=True)

    def descriptors(self) -> Sequence[ToolDescriptor]:
        manager = self._mgr()
        try:
            if not manager.is_available():
                return []
            discovered = manager.discovered_tools()
        except Exception:
            return []
        return [self._descriptor(tool) for tool in discovered]

    def _descriptor(self, tool: Any) -> ToolDescriptor:
        spec = spec_for_tool(tool.schema.name)
        aliases, result_limit = self._server_metadata(tool.schema.name)
        return ToolDescriptor(
            schema=tool.schema,
            timeout_sec=int(self._timeout()),
            origin=f"mcp:{tool.server_id}",
            surface=spec.surface,
            capability_class=spec.capability_class,
            safety_policy=spec.safety_policy,
            result_limit=result_limit,
            retrieval_aliases=aliases,
        )

    def _server_metadata(self, full_name: str) -> tuple[tuple[str, ...], int | None]:
        """Retrieval aliases and the result cap the owning server's settings entry declares.

        Both come from configuration rather than from the tool itself — an MCP server
        cannot describe how its results should be indexed or capped — so they are read
        through the manager rather than off the discovered tool.
        """
        manager = self._mgr()
        aliases: tuple[str, ...] = ()
        result_limit: int | None = None
        try:
            aliases = tuple(str(alias) for alias in manager.retrieval_aliases(full_name) or ())
        except Exception:
            aliases = ()
        try:
            configured = manager.tool_result_limit(full_name)
            if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
                # Settings validation caps this at the same ceiling; min() keeps the
                # descriptor inside it even if the two ever drift apart.
                result_limit = min(configured, RESULT_LIMIT_HARD_MAX)
        except Exception:
            result_limit = None
        return aliases, result_limit

    def get(self, name: str) -> ToolDescriptor | None:
        manager = self._mgr()
        requested = str(name or "").strip()
        try:
            if not manager.is_available():
                return None
            resolved = manager.resolve(requested)
            if resolved is None:
                return None
            for tool in manager.discovered_tools():
                if tool.full_name == requested:
                    return self._descriptor(tool)
        except Exception:
            return None
        # `resolve` is the authority on whether a name is dispatchable — the same check
        # the registry made. A name it knows but the discovery index does not still
        # gets a descriptor, with an empty schema rather than none at all.
        spec = spec_for_tool(requested)
        aliases, result_limit = self._server_metadata(requested)
        return ToolDescriptor(
            schema=ToolSchema(name=requested, description="", parameters={}),
            timeout_sec=int(self._timeout()),
            origin=f"mcp:{resolved[0]}",
            surface=spec.surface,
            capability_class=spec.capability_class,
            safety_policy=spec.safety_policy,
            result_limit=result_limit,
            retrieval_aliases=aliases,
        )

    def refuse_dispatch(self, name: str) -> str | None:
        """Nothing to withhold: ``resolve`` is the manager's whole answer about a name.

        A server that is down or unconfigured makes ``resolve`` return nothing, and this
        provider then has no basis to claim the name — the manager, not the provider, is what
        would have to remember it.
        """
        return None

    def execute(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
        manager = self._mgr()
        resolved = manager.resolve(str(name or "").strip())
        if resolved is None:
            return f"⚠️ Unknown MCP tool: {name}"
        server_id, original_name = resolved
        return manager.call_tool_sync(server_id, original_name, args, self._timeout())

    def close(self) -> None:
        # The manager's lifecycle belongs to the app lifespan, not to one provider instance.
        return
