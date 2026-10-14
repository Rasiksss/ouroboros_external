"""Executor test helpers extracted from test_cooperative_cancellation."""

from __future__ import annotations

from new_core.coordination.inprocess import NullWorkSignal
from ouroboros.agent.interfaces.owner_session import IOwnerSessionRepository
from ouroboros.agent.interfaces.startup_usage import IStartupUsageRepository
from ouroboros.agent_runtime.executor import TaskExecutor
from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.tool_confirmation.interfaces import IToolConfirmationRepository
from supervisor.queue import TaskQueue
from tests.helpers.agent_events import RecordingAgentEvents
from tests.helpers.agent_owner_session import sql_owner_session_repository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import (
    make_empty_extension_runtime,
    make_empty_skill_snapshot,
    make_skill_availability,
)
from tests.helpers.startup_usage import sql_startup_usage_repository
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


def make_queue(tmp_path, store):
    return TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=1,
        hard_timeout=60,
        audit_event_repository=sql_audit_event_repository(store.engine),
    )


def make_executor(
    tmp_path,
    store,
    *,
    session_repository: IOwnerSessionRepository | None = None,
    startup_usage_repository: IStartupUsageRepository | None = None,
    execution_memory_repository: IExecutionMemoryRepository | None = None,
    tool_confirmation_reader: IToolConfirmationRepository | None = None,
) -> TaskExecutor:
    return TaskExecutor(
        startup_usage_repository=startup_usage_repository
        if startup_usage_repository is not None
        else sql_startup_usage_repository(store.engine),
        session_repository=session_repository
        if session_repository is not None
        else sql_owner_session_repository(store.engine),
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_queue=make_queue(tmp_path, store),
        tasks_repo=store.tasks_repo,
        events=RecordingAgentEvents(),
        skill_snapshot=make_empty_skill_snapshot(),
        skill_availability=make_skill_availability(),
        extension_runtime=make_empty_extension_runtime(),
        runtime_id="test-runtime",
        work_signal=NullWorkSignal(),
        execution_memory_repository=execution_memory_repository
        if execution_memory_repository is not None
        else sql_execution_memory_repository(store.engine),
        tool_confirmation_reader=tool_confirmation_reader
        if tool_confirmation_reader is not None
        else sql_tool_confirmation_reader(store.engine),
        audit_event_repository=sql_audit_event_repository(store.engine),
    )
