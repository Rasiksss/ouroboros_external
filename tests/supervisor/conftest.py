"""Infrastructure fixtures for supervisor tests."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from tests.helpers.supervisor import bootstrap_supervisor_for_tests


@pytest.fixture
def supervisor(tmp_path):
    """A real runtime plus a context whose only fake is the outbound door."""
    from supervisor.runtime_context import reset_runtime_for_tests
    from transport.interfaces import routed_chat_id

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    sent: list[tuple[Any, str, dict[str, Any]]] = []
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        drive_root=tmp_path,
        result_repo=runtime.result_repo,
        bridge=runtime.bridge,
        state_repo=runtime.state_repo,
        orchestrator=runtime.orchestrator,
        task_queue=runtime.task_queue,
        agent_events=runtime.agent_events,
        cleanup_checkpoint_repository=runtime.cleanup_checkpoint_repository,
        working_retrieval_repository=runtime.working_retrieval_repository,
        execution_memory_repository=runtime.execution_memory_repository,
        tasks_repo=runtime.tasks_repo,
        tool_confirmation_reader=runtime.tool_confirmation_reader,
        conversations_repo=runtime.conversations_repo,
        send_with_budget=lambda text, **kwargs: sent.append((routed_chat_id(kwargs["scope"]), text, kwargs)),
    )
    try:
        yield SimpleNamespace(runtime=runtime, ctx=ctx, sent=sent, drive_root=tmp_path)
    finally:
        reset_runtime_for_tests()


@pytest.fixture
def stop_event():
    """A threading.Event the test can set to stop a loop."""
    return threading.Event()
