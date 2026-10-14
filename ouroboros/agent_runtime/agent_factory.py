"""Constructs one agent per run, for the runtime that holds the slot."""

from __future__ import annotations

import logging
import pathlib

from new_core.skills.catalog import SkillCatalogSnapshot
from new_core.skills.interfaces import IExtensionRuntime
from new_core.skills.surface import SkillAvailability
from new_core.tools.interfaces import ToolOutbound
from ouroboros.agent.core import OuroborosAgent
from ouroboros.agent.interfaces.owner_session import IOwnerSessionRepository
from ouroboros.agent.interfaces.startup_usage import IStartupUsageRepository
from ouroboros.agent_events.interfaces import IAgentEventPublisher
from ouroboros.observability.interfaces.audit_events import IAuditEventRepository
from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.tool_confirmation.interfaces import IToolConfirmationRepository

log = logging.getLogger(__name__)


class AgentFactory:
    """A constructor, not a cache: every run gets an agent of its own.

    Construction is field assignments, a queue and a graph assembly — measured in
    microseconds, with no file and no socket — so there is nothing to pool. What a cache
    bought was one shared mutable object, which is what made a lock around the body
    necessary; the mutable fields are per-run state by construction now.
    """

    def __init__(
        self,
        repo_dir: pathlib.Path,
        drive_root: pathlib.Path,
        events: IAgentEventPublisher,
        skill_snapshot: SkillCatalogSnapshot,
        skill_availability: SkillAvailability,
        extension_runtime: IExtensionRuntime,
        outbound: ToolOutbound | None = None,
        *,
        session_repository: IOwnerSessionRepository,
        startup_usage_repository: IStartupUsageRepository,
        execution_memory_repository: IExecutionMemoryRepository,
        audit_event_repository: IAuditEventRepository,
        tool_confirmation_reader: IToolConfirmationRepository,
    ) -> None:
        self.repo_dir = pathlib.Path(repo_dir)
        self.drive_root = pathlib.Path(drive_root)
        self.events = events
        self.skill_snapshot = skill_snapshot
        self.skill_availability = skill_availability
        self.extension_runtime = extension_runtime
        self.session_repository = session_repository
        self.startup_usage_repository = startup_usage_repository
        self.execution_memory_repository = execution_memory_repository
        self.audit_event_repository = audit_event_repository
        self.tool_confirmation_reader = tool_confirmation_reader
        #: Handed to every run this factory builds, from the composition root that owns
        #: the gateway. The factory names one half of the seam and never reaches for it.
        self.outbound = outbound

    def create(self) -> OuroborosAgent:
        """One agent for one run. Nothing it holds outlives the task it was built for."""
        from ouroboros.agent import make_agent

        return make_agent(
            repo_dir=str(self.repo_dir),
            drive_root=str(self.drive_root),
            skill_snapshot=self.skill_snapshot,
            skill_availability=self.skill_availability,
            extension_runtime=self.extension_runtime,
            session_repository=self.session_repository,
            startup_usage_repository=self.startup_usage_repository,
            execution_memory_repository=self.execution_memory_repository,
            tool_confirmation_reader=self.tool_confirmation_reader,
            events=self.events,
            outbound=self.outbound,
            audit_event_repository=self.audit_event_repository,
        )


__all__ = ["AgentFactory"]
