"""Wire supervisor runtime components into a single SupervisorRuntime."""

from __future__ import annotations

import pathlib
from typing import Any

from new_core.coordination.interfaces import IWorkSignal
from ouroboros.agent_events.interfaces import IAgentEventConsumer
from ouroboros.observability.infra.repositories.audit_events.repository import SqlAuditEventRepository
from ouroboros.storage.core_memory.scratchpad.infra.sql import SqlScratchpad
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.tool_confirmation.repository import SqlToolConfirmationRepository
from supervisor.infra.repositories.cleanup_checkpoint.repo import SqlCleanupCheckpointRepository
from supervisor.infra.repositories.owner_state.repo import SqlOwnerStateRepository
from supervisor.infra.repositories.runtime_sessions.repo import SqlRuntimeSessionRepository
from supervisor.infra.repositories.usage.repo import SqlUsageRepository
from supervisor.infra.repositories.working_retrieval.repo import SqlWorkingRetrievalRepository
from supervisor.message_bus import LocalChatBridge
from supervisor.orchestrator.orchestrator import TaskOrchestrator
from supervisor.queue import TaskQueue
from supervisor.runtime_context import SupervisorRuntime
from supervisor.state import RuntimeStateRepository, configured_owner_state_scope, init_state_for_repo
from transport.interfaces import IMessageGateway


def build_supervisor_runtime(
    *,
    settings: dict[str, Any],
    drive_root: pathlib.Path,
    repo_dir: pathlib.Path,
    gateway: IMessageGateway,
    bridge: LocalChatBridge,
    agent_events: IAgentEventConsumer,
    work_signal: IWorkSignal,
) -> SupervisorRuntime:
    soft_timeout = int(settings.get("OUROBOROS_SOFT_TIMEOUT_SEC", 600))
    hard_timeout = int(settings.get("OUROBOROS_HARD_TIMEOUT_SEC", 1800))

    drive_root = pathlib.Path(drive_root)
    repo_dir = pathlib.Path(repo_dir)

    store = runtime_persistence.current
    transaction_manager = runtime_persistence.current.transaction_manager
    audit_event_repository = SqlAuditEventRepository(transaction_manager)
    usage_repository = SqlUsageRepository(store.engine)
    tool_confirmation_reader = SqlToolConfirmationRepository(
        transaction_manager,
        audit_event_repository=audit_event_repository,
        max_running_root_jobs_per_owner=int(settings.get("OUROBOROS_MAX_RUNNING_ROOT_JOBS_PER_OWNER", 3)),
    )
    # The instance's configured owner: the composition root holds no conversation, and
    # every caller that does addresses its own owner through ``for_scope``.
    state_repo = RuntimeStateRepository(
        drive_root,
        owner_state_repository=SqlOwnerStateRepository(store.engine),
        scope=configured_owner_state_scope(),
        audit_event_repository=audit_event_repository,
        usage_repository=usage_repository,
    )
    session_repository = SqlRuntimeSessionRepository(store.engine)
    state_repo.sync_runtime_session(session_repository, source="dialogs")
    task_queue = TaskQueue(
        runtime_persistence.current.tasks_repo,
        drive_root,
        work_signal=work_signal,
        soft_timeout=soft_timeout,
        hard_timeout=hard_timeout,
        max_running_root_jobs_per_owner=int(settings.get("OUROBOROS_MAX_RUNNING_ROOT_JOBS_PER_OWNER", 3)),
        audit_event_repository=audit_event_repository,
    )
    init_state_for_repo(state_repo)
    orchestrator = TaskOrchestrator(
        conversations_repo=runtime_persistence.current.conversations_repo,
        tool_confirmation_reader=tool_confirmation_reader,
        tasks_repo=runtime_persistence.current.tasks_repo,
        create_root_task=runtime_persistence.current.create_root_task,
        task_queue=task_queue,
        state_repo=state_repo,
    )
    return SupervisorRuntime(
        result_repo=runtime_persistence.current.result_repo,
        conversations_repo=runtime_persistence.current.conversations_repo,
        drive_root=drive_root,
        repo_dir=repo_dir,
        state_repo=state_repo,
        task_queue=task_queue,
        agent_events=agent_events,
        orchestrator=orchestrator,
        soft_timeout=soft_timeout,
        hard_timeout=hard_timeout,
        bridge=bridge,
        gateway=gateway,
        usage_repository=usage_repository,
        cleanup_checkpoint_repository=SqlCleanupCheckpointRepository(store.engine),
        working_retrieval_repository=SqlWorkingRetrievalRepository(
            store.engine, scratchpad=SqlScratchpad(store.engine)
        ),
        execution_memory_repository=runtime_persistence.current.memory_repo,
        tasks_repo=runtime_persistence.current.tasks_repo,
        tool_confirmation_reader=tool_confirmation_reader,
    )


__all__ = ["build_supervisor_runtime"]
