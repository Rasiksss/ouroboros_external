"""Process-local supervisor runtime registry."""

from __future__ import annotations

import pathlib
import threading
from dataclasses import dataclass
from typing import Any

from supervisor.orchestrator.orchestrator import TaskOrchestrator
from supervisor.queue import TaskQueue
from supervisor.workers import TaskExecutor

_RUNTIME_LOCK = threading.Lock()
_RUNTIME: SupervisorRuntime | None = None


@dataclass
class SupervisorRuntime:
    drive_root: pathlib.Path
    repo_dir: pathlib.Path
    state_repo: Any
    store: Any
    task_queue: TaskQueue
    executor: TaskExecutor
    orchestrator: TaskOrchestrator
    soft_timeout: int
    hard_timeout: int
    bridge: Any


def set_runtime(runtime: SupervisorRuntime) -> None:
    global _RUNTIME
    with _RUNTIME_LOCK:
        _RUNTIME = runtime
    from supervisor.message_bus import configure_outbound_sender

    configure_outbound_sender(None)


def try_get_runtime() -> SupervisorRuntime | None:
    with _RUNTIME_LOCK:
        return _RUNTIME


def require_runtime() -> SupervisorRuntime:
    runtime = try_get_runtime()
    if runtime is None:
        raise RuntimeError("Supervisor runtime is not initialized")
    return runtime


def try_get_executor() -> TaskExecutor | None:
    runtime = try_get_runtime()
    return runtime.executor if runtime is not None else None


def try_get_orchestrator() -> TaskOrchestrator | None:
    runtime = try_get_runtime()
    return runtime.orchestrator if runtime is not None else None


def try_get_bridge() -> Any | None:
    runtime = try_get_runtime()
    return runtime.bridge if runtime is not None else None


def reset_runtime_for_tests() -> None:
    global _RUNTIME
    with _RUNTIME_LOCK:
        _RUNTIME = None
    from supervisor.message_bus import configure_outbound_sender

    configure_outbound_sender(None)


__all__ = [
    "SupervisorRuntime",
    "require_runtime",
    "reset_runtime_for_tests",
    "set_runtime",
    "try_get_bridge",
    "try_get_executor",
    "try_get_orchestrator",
    "try_get_runtime",
]
