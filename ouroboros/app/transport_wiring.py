"""What the transports need from above the seam, implemented by the root.

``transport/`` may not import ``supervisor``, ``new_core`` or ``ouroboros.app``,
so every collaborator a transport declares as an interface is implemented here
and handed to it at startup.
"""

from __future__ import annotations

import logging

from transport.a2a.interfaces import AgentSkillSpec, IAgentSkillCatalog, ISupervisorReadiness

log = logging.getLogger(__name__)


class CoreToolSkillCatalog(IAgentSkillCatalog):
    """The agent card's skills: this process's core tools, read on every request.

    The process-wide catalog, not one assembled here, so the card cannot
    disagree with what tasks actually run against — and not a snapshot either:
    the extension, MCP and A2A providers behind it gain and lose tools while the
    process runs, and an empty answer only means the lifespan has not wired it
    yet.
    """

    def skills(self) -> list[AgentSkillSpec]:
        from new_core.identity import ServiceJob
        from new_core.tools.catalog import tool_catalog
        from ouroboros.contracts.owner_scope import service_identity
        from ouroboros.policy.capabilities import CORE_TOOL_NAMES
        from ouroboros.tools.catalog import spec_for_tool

        if not tool_catalog.providers:
            return []
        specs = []
        scope = service_identity(job=ServiceJob.AGENT_CARD).as_scope()
        for descriptor in tool_catalog.descriptors(scope):
            name = descriptor.name
            if not name or name not in CORE_TOOL_NAMES:
                continue
            if spec_for_tool(name).surface == "excluded":  # disabled via OUROBOROS_DISABLED_TOOLS
                continue
            specs.append(
                AgentSkillSpec(
                    id=name,
                    name=name,
                    description=descriptor.schema.description or "",
                    tags=(name.split("_")[0] if "_" in name else "tool",),
                )
            )
        return specs


class SupervisorReadinessGate(ISupervisorReadiness):
    """Waits on the readiness the supervisor thread signals at startup."""

    async def wait_ready(self, *, timeout_sec: float) -> None:
        from ouroboros.app.supervisor_readiness import wait_for_supervisor_ready

        # No bridge requirement: A2A submits onto the inbox, which the loop drains
        # whether or not a chat bridge was wired.
        await wait_for_supervisor_ready(timeout_sec=timeout_sec, require_bridge=False)


__all__ = [
    "CoreToolSkillCatalog",
    "SupervisorReadinessGate",
]
