"""Remote A2A agents: one tool per configured agent, plus two process-wide task tools.

One tool per *agent*, never one per skill: the protocol offers no way to address a published
skill, so a per-skill tool would send the identical request to the identical URL. Follow-up is
served by two global tools taking an ``agent_id``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from typing import TYPE_CHECKING

from new_core.tools.interfaces import IToolProvider, ToolDescriptor
from new_core.tools.loop_bridge import call_on_loop_sync
from new_core.tools.runtimes import INLINE_RUNTIME
from new_core.tools.schema import ToolSchema
from ouroboros.egress.a2a.connection import tool_ceiling_sec
from ouroboros.egress.a2a.transports import CARD_CONNECT_TIMEOUT_SEC
from ouroboros.tools.catalog import RESULT_LIMIT_HARD_MAX, spec_for_tool

if TYPE_CHECKING:
    from new_core.identity import OwnerScope
    from new_core.tools.context import ToolContext
    from ouroboros.egress.a2a.connection import A2aRemoteAgentConnection
    from ouroboros.egress.a2a.dependencies import A2aEgressDependencies
    from ouroboros.egress.a2a.manager import A2aManager
    from ouroboros.storage.settings.schema import A2aRemoteAgentEntry

log = logging.getLogger(__name__)

TASK_STATUS_TOOL = "a2a_task_status"
TASK_CANCEL_TOOL = "a2a_task_cancel"
_TASK_STATUS_SCHEMA = ToolSchema(
    name=TASK_STATUS_TOOL,
    description=(
        "Read a remote A2A task started earlier by an a2a__* tool: its state, and its result "
        "once it has finished. With wait_sec > 0 the call waits that long for the task to "
        "finish before answering; with 0 it returns the current state immediately."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "The remote agent the task belongs to."},
            "task_id": {
                "type": "string",
                "description": "The task_id exactly as the result that started the task reported it, unchanged.",
            },
            "wait_sec": {
                "type": "number",
                "description": "Seconds to wait for the task to finish. 0 (default) reads the current state.",
            },
        },
        "required": ["agent_id", "task_id"],
    },
)

_TASK_CANCEL_SCHEMA = ToolSchema(
    name=TASK_CANCEL_TOOL,
    description=(
        "Ask a remote A2A agent to cancel a task it is running. Use when the work is no longer "
        "needed; whatever the agent already produced stays readable with a2a_task_status."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "The remote agent the task belongs to."},
            "task_id": {
                "type": "string",
                "description": "The task_id to cancel, exactly as the result that started the task reported it.",
            },
        },
        "required": ["agent_id", "task_id"],
    },
)


class A2aToolProvider(IToolProvider):
    """Tools reached through the outbound A2A manager, which is the authority on what is
    connected: this provider holds no catalog of its own and every read goes through it live.

    ``INLINE_RUNTIME`` because ``execute`` returns the manager's coroutine un-awaited: the
    remote call already lives on the lifespan loop, and a worker thread would only stand and
    block while it ran there.
    """

    default_runtime = INLINE_RUNTIME

    def __init__(
        self,
        manager: A2aManager,
        timeout_sec: int,
        *,
        agents: Sequence[A2aRemoteAgentEntry] | None,
        loop: asyncio.AbstractEventLoop | None,
        dependencies: A2aEgressDependencies,
        egress_enabled: bool = False,
    ) -> None:
        if agents is None and loop is None:
            self._owned_initialization: tuple[list[A2aRemoteAgentEntry], asyncio.AbstractEventLoop] | None = None
        elif agents is not None and loop is not None:
            self._owned_initialization = (list(agents), loop)
        else:
            raise ValueError("agents and loop must both be supplied for provider-owned initialization")
        self._manager = manager
        self._dependencies = dependencies
        self._egress_enabled = egress_enabled
        self._timeout_sec = timeout_sec
        self._ceiling_sec = tool_ceiling_sec(timeout_sec)

    def refresh(self) -> None:
        owned_initialization = self._owned_initialization
        if owned_initialization is None:
            return
        agents, loop = owned_initialization
        if not self._egress_enabled:
            return
        if not agents:
            log.info("A2A egress enabled, but security_config/a2a.py lists no agents; egress manager not initialized")
            return
        if self._manager.is_available():
            return
        try:
            call_on_loop_sync(
                loop,
                lambda: self._manager.initialize(agents, loop, self._egress_enabled, self._dependencies),
                timeout_sec=CARD_CONNECT_TIMEOUT_SEC,
            )
            log.info("A2A egress manager initialized agents=%d", len(self._manager.connections()))
        except Exception:
            log.warning("A2A provider refresh failed; continuing without A2A tools", exc_info=True)

    # --- catalog reads --------------------------------------------------------------------

    def descriptors(self, scope: OwnerScope) -> Sequence[ToolDescriptor]:
        del scope
        connections = self._connections()
        if not connections:
            # No agent means no task to ask after either.
            return []
        return [self._agent_descriptor(connection) for connection in connections] + [
            self._companion_descriptor(TASK_STATUS_TOOL),
            self._companion_descriptor(TASK_CANCEL_TOOL),
        ]

    def get(self, name: str, scope: OwnerScope) -> ToolDescriptor | None:
        del scope
        requested = str(name or "").strip()
        if not requested or not self._manager.is_available():
            return None
        if requested in (TASK_STATUS_TOOL, TASK_CANCEL_TOOL):
            # The task tools exist only alongside at least one agent to ask about.
            return self._companion_descriptor(requested) if self._manager.connections() else None
        connection = self._manager.connection_for_tool(requested)
        return self._agent_descriptor(connection) if connection is not None else None

    def refuse_dispatch(self, name: str, scope: OwnerScope) -> str | None:
        """Nothing to withhold: the manager's connection list is this provider's whole answer."""
        del name, scope
        return None

    def _connections(self) -> list[A2aRemoteAgentConnection]:
        if not self._manager.is_available():
            return []
        return self._manager.connections()

    # --- descriptors ----------------------------------------------------------------------

    def _agent_descriptor(self, connection: A2aRemoteAgentConnection) -> ToolDescriptor:
        spec = spec_for_tool(connection.tool_name)
        entry = connection.entry
        limit = entry.tool_result_limit
        return ToolDescriptor(
            schema=connection.schema,
            timeout_sec=self._ceiling_sec,
            origin=f"a2a:{entry.id}",
            surface=spec.surface,
            # Not the ``read_only`` default a nameless tool would inherit: this one hands
            # arbitrary work to a process outside this one.
            capability_class="external_network",
            safety_policy=spec.safety_policy,
            result_limit=min(limit, RESULT_LIMIT_HARD_MAX) if limit > 0 else None,
            retrieval_aliases=tuple(entry.retrieval_aliases),
            parallel_safe=False,
        )

    def _companion_descriptor(self, name: str) -> ToolDescriptor:
        spec = spec_for_tool(name)
        return ToolDescriptor(
            schema=_TASK_STATUS_SCHEMA if name == TASK_STATUS_TOOL else _TASK_CANCEL_SCHEMA,
            timeout_sec=self._ceiling_sec,
            # Process-wide, so no single agent to name — but still `a2a:`, because the gates
            # that select remote tools by origin must cover these too.
            origin="a2a:*",
            surface=spec.surface,
            capability_class="external_network",
            safety_policy=spec.safety_policy,
            parallel_safe=name == TASK_STATUS_TOOL,
        )

    # --- execution ------------------------------------------------------------------------

    def execute(self, ctx: ToolContext, name: str, args: dict[str, object]) -> str | Awaitable[str]:
        requested = str(name or "").strip()
        timeout = self._timeout_sec

        if requested == TASK_STATUS_TOOL:
            return self._manager.task_status(
                ctx.scope,
                str(args.get("agent_id", "") or ""),
                str(args.get("task_id", "") or ""),
                self._as_float(args.get("wait_sec"), default=0.0),
                timeout,
            )
        if requested == TASK_CANCEL_TOOL:
            return self._manager.task_cancel(
                ctx.scope,
                str(args.get("agent_id", "") or ""),
                str(args.get("task_id", "") or ""),
                timeout,
            )

        agent_id = self._manager.resolve(requested)
        if agent_id is None:
            return f"⚠️ Unknown A2A tool: {name}"
        return self._manager.call_tool(ctx.scope, agent_id, args, timeout)

    def _as_float(self, value: object, *, default: float) -> float:
        if isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return default
        return default
