"""Wire the runtime's parts into one :class:`AgentRuntime`.

It builds its own queue facade rather than borrowing the supervisor's: the two components
share records, not objects, and that is the whole of what has to still be true when they
stop sharing a process. The store underneath is the one the composition root composed —
asking for it is not sharing an object, it is naming the process's one database.
"""

from __future__ import annotations

import pathlib
import threading
from typing import Any

from new_core.coordination.interfaces import ITaskStopConsumer, IWorkSignal
from new_core.skills.catalog import SkillCatalogSnapshot
from new_core.skills.interfaces import IExtensionRuntime
from new_core.skills.surface import SkillAvailability
from new_core.tools.interfaces import ToolOutbound
from ouroboros.agent.infra.repositories.owner_session.repo import SqlOwnerSessionRepository
from ouroboros.agent.infra.repositories.startup_usage.repo import SqlStartupUsageRepository
from ouroboros.agent_events.interfaces import IAgentEventPublisher
from ouroboros.agent_runtime.executor import TaskExecutor
from ouroboros.agent_runtime.identity import mint_runtime_id
from ouroboros.agent_runtime.runtime import RUNTIME_POLL_INTERVAL_SEC, AgentRuntime
from ouroboros.observability.health import LoopHeartbeat
from ouroboros.observability.infra.repositories.audit_events.repository import SqlAuditEventRepository
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.tool_confirmation.repository import SqlToolConfirmationRepository
from supervisor.queue import HEARTBEAT_STALE_SEC, TaskQueue

#: The window after which the fleet treats a run's holder as gone, held against the loop that
#: claims for it: a claim loop silent for longer is one whose runs are already being reclaimed.
CLAIM_LOOP_STALL_SEC = HEARTBEAT_STALE_SEC


def build_agent_runtime(
    *,
    settings: dict[str, Any],
    drive_root: pathlib.Path,
    repo_dir: pathlib.Path,
    events: IAgentEventPublisher,
    skill_snapshot: SkillCatalogSnapshot,
    skill_availability: SkillAvailability,
    extension_runtime: IExtensionRuntime,
    outbound: ToolOutbound,
    work_signal: IWorkSignal,
    stop_event: threading.Event,
    heartbeat: LoopHeartbeat,
    task_stops: ITaskStopConsumer,
    poll_interval_sec: float = RUNTIME_POLL_INTERVAL_SEC,
) -> AgentRuntime:
    drive_root = pathlib.Path(drive_root)
    repo_dir = pathlib.Path(repo_dir)
    store = runtime_persistence.current.tasks_repo
    engine = runtime_persistence.current.engine
    transaction_manager = runtime_persistence.current.transaction_manager
    audit_event_repository = SqlAuditEventRepository(transaction_manager)
    task_queue = TaskQueue(
        store,
        drive_root,
        # Held for the shape rather than for a caller: a runtime enqueues nothing, because
        # a run asking for a subtask asks the supervisor, which is what enqueuing is. The
        # signal that matters here is the one the executor raises when a lane comes free.
        work_signal=work_signal,
        soft_timeout=int(settings.get("OUROBOROS_SOFT_TIMEOUT_SEC", 600)),
        hard_timeout=int(settings.get("OUROBOROS_HARD_TIMEOUT_SEC", 1800)),
        max_running_root_jobs_per_owner=int(settings.get("OUROBOROS_MAX_RUNNING_ROOT_JOBS_PER_OWNER", 3)),
        audit_event_repository=audit_event_repository,
    )
    executor = TaskExecutor(
        repo_dir=repo_dir,
        drive_root=drive_root,
        task_queue=task_queue,
        tasks_repo=store,
        events=events,
        skill_snapshot=skill_snapshot,
        skill_availability=skill_availability,
        extension_runtime=extension_runtime,
        session_repository=SqlOwnerSessionRepository(engine),
        startup_usage_repository=SqlStartupUsageRepository(engine),
        execution_memory_repository=runtime_persistence.current.memory_repo,
        tool_confirmation_reader=SqlToolConfirmationRepository(
            transaction_manager,
            audit_event_repository=audit_event_repository,
            max_running_root_jobs_per_owner=int(settings.get("OUROBOROS_MAX_RUNNING_ROOT_JOBS_PER_OWNER", 3)),
        ),
        runtime_id=mint_runtime_id(),
        work_signal=work_signal,
        outbound=outbound,
        audit_event_repository=audit_event_repository,
    )
    return AgentRuntime(
        executor,
        work_signal=work_signal,
        stop_event=stop_event,
        heartbeat=heartbeat,
        task_stops=task_stops,
        poll_interval_sec=poll_interval_sec,
    )


__all__ = ["CLAIM_LOOP_STALL_SEC", "build_agent_runtime"]
