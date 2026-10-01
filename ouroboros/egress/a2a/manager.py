"""Process-wide outbound A2A manager — a lifespan-initialized singleton.

Same shape as :mod:`ouroboros.mcp.manager`: a module-level instance whose ``initialize()`` the
app lifespan calls once, and which behaves as an empty manager until then. What it adds is
resolution by *agent* id as well as by tool name, for the two process-wide task tools.
"""

from __future__ import annotations

import asyncio
import logging
from asyncio import AbstractEventLoop
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from new_core.tools.loop_bridge import BridgeTimeout, call_on_loop
from ouroboros.egress.a2a.connection import (
    BRIDGE_MARGIN_SEC,
    A2aRemoteAgentConnection,
    format_a2a_error,
    format_a2a_timeout,
)
from ouroboros.storage.settings.schema import A2aRemoteAgentEntry

log = logging.getLogger(__name__)

_FALSY_ARGS = frozenset({"false", "0", "no", "off", "none", "null"})
_TRUTHY_ARGS = frozenset({"true", "1", "yes", "on"})


class A2aManager:
    """Owns connections to all configured remote A2A agents for the process lifetime."""

    def __init__(self, loop: AbstractEventLoop | None = None) -> None:
        self._loop = loop
        self._connections: dict[str, A2aRemoteAgentConnection] = {}
        self._tool_index: dict[str, A2aRemoteAgentConnection] = {}
        self._initialized = False

    async def initialize(
        self,
        agents: list[A2aRemoteAgentEntry],
        loop: AbstractEventLoop,
        egress_enabled: bool,
    ) -> None:
        """Connect to every enabled agent. Call once, from lifespan.

        ``egress_enabled`` is the caller's reading of ``A2A_EGRESS_ENABLED``: false means
        there is nothing to connect, so the manager stays empty.
        """
        if self._initialized:
            raise RuntimeError("A2aManager.initialize() was already called")

        self._loop = loop
        enabled: list[A2aRemoteAgentEntry] = []
        seen_ids: set[str] = set()
        for raw in agents if egress_enabled else []:
            # One normalized id from here down: it keys the connection map, names the tool,
            # and is what the model sends back — a stray space in settings would otherwise
            # advertise an agent no dispatch could resolve.
            entry = raw.model_copy(update={"id": raw.id.strip(), "url": raw.url.strip()})
            if not (entry.enabled and entry.id and entry.url):
                continue
            if entry.id in seen_ids:
                log.warning("Duplicate A2A agent id %s in settings; keeping the first entry", entry.id)
                continue
            seen_ids.add(entry.id)
            enabled.append(entry)

        results = await asyncio.gather(
            *(self._connect_one(entry) for entry in enabled),
            return_exceptions=True,
        )
        # Assembled in configuration order rather than in the order peers answered, so the
        # tool list, the prompt prefix built from it, and the winner of a name collision do
        # not depend on network latency.
        for entry, result in zip(enabled, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("Skipping A2A agent %s after connection failure: %s", entry.id, result)
                continue
            self._connections[entry.id] = result

        for connection in self._connections.values():
            existing = self._tool_index.get(connection.tool_name)
            if existing is not None:
                log.warning(
                    "A2A tool name collision: %s from agent %s already maps to agent %s; keeping the first",
                    connection.tool_name,
                    connection.entry.id,
                    existing.entry.id,
                )
                continue
            self._tool_index[connection.tool_name] = connection

        self._initialized = True

    async def _connect_one(self, entry: A2aRemoteAgentEntry) -> A2aRemoteAgentConnection:
        connection = A2aRemoteAgentConnection(entry)
        try:
            await connection.connect()
        except BaseException:
            # Includes the cancellation of a lifespan cut short: nothing else has a reference
            # to this connection yet, so closing it here is the only chance there is.
            await connection.aclose()
            raise
        return connection

    # --- reads ---------------------------------------------------------------------------

    def is_available(self) -> bool:
        """Initialized, which for a disabled egress means initialized with no agents.
        Bound at startup, so turning egress on or off needs a restart."""
        return self._initialized

    def connections(self) -> list[A2aRemoteAgentConnection]:
        """Every connected agent, in the order their tool names were claimed."""
        return list(self._tool_index.values())

    def connection_for_tool(self, tool_name: str) -> A2aRemoteAgentConnection | None:
        return self._tool_index.get(str(tool_name or "").strip())

    def resolve(self, tool_name: str) -> str | None:
        """The agent id behind an ``a2a__{agent}`` tool name."""
        connection = self.connection_for_tool(tool_name)
        return connection.entry.id if connection is not None else None

    def connection_for(self, agent_id: str) -> A2aRemoteAgentConnection | None:
        return self._connections.get(str(agent_id or "").strip())

    # --- calls ---------------------------------------------------------------------------

    async def call_tool(self, agent_id: str, arguments: Mapping[str, object], timeout_sec: float) -> str:
        """Send one message to a remote agent, from whatever loop the agent runs on."""
        connection = self.connection_for(agent_id)
        if connection is None:
            return format_a2a_error(agent_id, f"unknown remote agent {agent_id!r}")
        return await self._bridge(
            agent_id,
            timeout_sec,
            lambda: connection.call(
                str(arguments.get("message", "") or ""),
                wait=_as_bool(arguments.get("wait"), default=True),
                task_id=str(arguments.get("task_id", "") or ""),
                timeout_sec=timeout_sec,
            ),
        )

    async def task_status(self, agent_id: str, task_id: str, wait_sec: float, timeout_sec: float) -> str:
        connection = self.connection_for(agent_id)
        if connection is None:
            return format_a2a_error(agent_id, f"unknown remote agent {agent_id!r}")
        bounded = max(0.0, min(float(wait_sec), float(timeout_sec)))
        return await self._bridge(
            agent_id,
            timeout_sec,
            lambda: connection.status(task_id, wait_sec=bounded, timeout_sec=timeout_sec),
        )

    async def task_cancel(self, agent_id: str, task_id: str, timeout_sec: float) -> str:
        connection = self.connection_for(agent_id)
        if connection is None:
            return format_a2a_error(agent_id, f"unknown remote agent {agent_id!r}")
        return await self._bridge(
            agent_id,
            timeout_sec,
            lambda: connection.cancel(task_id, timeout_sec=timeout_sec),
        )

    async def _bridge(
        self,
        agent_id: str,
        timeout_sec: float,
        make_coro: Callable[[], Coroutine[Any, Any, str]],
    ) -> str:
        if self._loop is None:
            return format_a2a_error(agent_id, "the A2A manager is not initialized")
        try:
            return await call_on_loop(self._loop, make_coro, timeout_sec=timeout_sec, grace_sec=BRIDGE_MARGIN_SEC)
        except BridgeTimeout:
            return format_a2a_timeout(agent_id, f"no answer within {timeout_sec}s waiting for the remote agent")
        except Exception as exc:
            return format_a2a_error(agent_id, exc)

    # --- lifecycle -----------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Close every connection this manager opened, initialized or not.

        Not guarded on ``_initialized``: a lifespan cancelled mid-``initialize()`` leaves
        connections behind without ever having set it.
        """
        connections = list(self._connections.values())
        self._connections.clear()
        self._tool_index.clear()
        self._initialized = False
        if connections:
            await asyncio.gather(*(connection.aclose() for connection in connections), return_exceptions=True)


def _as_bool(value: object, *, default: bool) -> bool:
    """Anything the model did not clearly say means the documented default — ``null`` for an
    optional parameter included, which is how models routinely spell "not set"."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _FALSY_ARGS:
            return False
        if text in _TRUTHY_ARGS:
            return True
        return default
    return bool(value)


a2a_manager = A2aManager()

__all__ = ["A2aManager", "a2a_manager"]
