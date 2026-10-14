"""Process-local supervisor runtime registry."""

from __future__ import annotations

import pathlib
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ouroboros.agent_events.interfaces import IAgentEventConsumer
from ouroboros.storage.conversations.interfaces import IConversationRepository
from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.result_delivery.interfaces import IResultDeliveryRepository
from ouroboros.storage.tasks.interfaces import ITaskRepository
from ouroboros.storage.tool_confirmation.interfaces import IToolConfirmationRepository
from supervisor.interfaces.cleanup_checkpoint import ICleanupCheckpointRepository
from supervisor.interfaces.usage import IUsageRepository
from supervisor.interfaces.working_retrieval import IWorkingRetrievalRepository
from transport.interfaces import IMessageGateway

if TYPE_CHECKING:
    from supervisor.message_bus import LocalChatBridge
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import TaskQueue
    from supervisor.state import RuntimeStateRepository

_RUNTIME_LOCK = threading.Lock()
_RUNTIME: SupervisorRuntime | None = None


@dataclass
class SupervisorRuntime:
    """What the supervisor holds. It runs no task and holds nothing that does.

    There is no identity here: the identity on a record names the component that is
    running the task, which after the split is never this one.
    """

    drive_root: pathlib.Path
    repo_dir: pathlib.Path
    state_repo: RuntimeStateRepository
    task_queue: TaskQueue
    #: What a run's reports are read from. Read-only: the supervisor cannot publish onto it.
    agent_events: IAgentEventConsumer
    orchestrator: TaskOrchestrator
    soft_timeout: int
    hard_timeout: int
    bridge: LocalChatBridge
    gateway: IMessageGateway
    usage_repository: IUsageRepository
    cleanup_checkpoint_repository: ICleanupCheckpointRepository
    working_retrieval_repository: IWorkingRetrievalRepository
    execution_memory_repository: IExecutionMemoryRepository
    conversations_repo: IConversationRepository
    result_repo: IResultDeliveryRepository
    tool_confirmation_reader: IToolConfirmationRepository
    tasks_repo: ITaskRepository


def set_runtime(runtime: SupervisorRuntime) -> None:
    global _RUNTIME
    with _RUNTIME_LOCK:
        _RUNTIME = runtime


def try_get_runtime() -> SupervisorRuntime | None:
    with _RUNTIME_LOCK:
        return _RUNTIME


def require_runtime() -> SupervisorRuntime:
    runtime = try_get_runtime()
    if runtime is None:
        raise RuntimeError("Supervisor runtime is not initialized")
    return runtime


def try_get_orchestrator() -> TaskOrchestrator | None:
    runtime = try_get_runtime()
    return runtime.orchestrator if runtime is not None else None


def try_get_bridge() -> LocalChatBridge | None:
    runtime = try_get_runtime()
    return runtime.bridge if runtime is not None else None


def try_get_gateway() -> IMessageGateway | None:
    runtime = try_get_runtime()
    return runtime.gateway if runtime is not None else None


def reset_runtime_for_tests() -> None:
    global _RUNTIME
    with _RUNTIME_LOCK:
        _RUNTIME = None


__all__ = [
    "SupervisorRuntime",
    "require_runtime",
    "reset_runtime_for_tests",
    "set_runtime",
    "try_get_bridge",
    "try_get_gateway",
    "try_get_orchestrator",
    "try_get_runtime",
]
