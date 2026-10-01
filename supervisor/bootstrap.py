"""Wire supervisor runtime components into a single SupervisorRuntime."""

from __future__ import annotations

import pathlib
from typing import Any

from ouroboros.storage.runtime_store import init_runtime_store
from supervisor.message_bus import LocalChatBridge
from supervisor.orchestrator.orchestrator import TaskOrchestrator
from supervisor.queue import TaskQueue
from supervisor.runtime_context import SupervisorRuntime
from supervisor.state import RuntimeStateRepository, init_state_for_repo
from supervisor.workers import TaskExecutor


def build_supervisor_runtime(
    *,
    settings: dict[str, Any],
    drive_root: pathlib.Path,
    repo_dir: pathlib.Path,
    bridge: Any | None = None,
) -> SupervisorRuntime:
    soft_timeout = int(settings.get("OUROBOROS_SOFT_TIMEOUT_SEC", 600))
    hard_timeout = int(settings.get("OUROBOROS_HARD_TIMEOUT_SEC", 1800))

    drive_root = pathlib.Path(drive_root)
    repo_dir = pathlib.Path(repo_dir)
    chat_bridge = bridge if bridge is not None else LocalChatBridge(settings)

    store = init_runtime_store(drive_root)
    state_repo = RuntimeStateRepository(drive_root, store=store)
    state_repo.sync_runtime_session(store, source="dialogs")
    task_queue = TaskQueue(
        store,
        drive_root,
        soft_timeout=soft_timeout,
        hard_timeout=hard_timeout,
        state_repo=state_repo,
    )
    executor = TaskExecutor(
        repo_dir=repo_dir,
        drive_root=drive_root,
        task_queue=task_queue,
        store=store,
    )
    executor.set_media_senders(
        image=getattr(chat_bridge, "send_image", None), file=getattr(chat_bridge, "send_file", None)
    )
    init_state_for_repo(state_repo)
    orchestrator = TaskOrchestrator(
        store=store,
        task_queue=task_queue,
        state_repo=state_repo,
        agent_getter=executor.get_agent,
    )
    return SupervisorRuntime(
        drive_root=drive_root,
        repo_dir=repo_dir,
        state_repo=state_repo,
        store=store,
        task_queue=task_queue,
        executor=executor,
        orchestrator=orchestrator,
        soft_timeout=soft_timeout,
        hard_timeout=hard_timeout,
        bridge=chat_bridge,
    )


__all__ = ["build_supervisor_runtime"]
