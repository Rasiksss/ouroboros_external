"""The supervisor after memory left it: one lane, and two retired task types.

Distillation and consolidation both run on the background memory loop now, off
the task queue. What is left to pin is that neither type can ever be executed
again from a row an earlier version wrote.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from new_core.coordination.inprocess import NullWorkSignal
from tests.helpers.agent_events import RecordingAgentEvents
from tests.helpers.agent_owner_session import sql_owner_session_repository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import (
    TEST_SCOPE,
    make_empty_extension_runtime,
    make_empty_skill_snapshot,
    make_skill_availability,
)
from tests.helpers.startup_usage import sql_startup_usage_repository
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader

_RETIRED = ("memory_distillation", "context_consolidation")


class _PendingFuture:
    def done(self):
        return False

    def add_done_callback(self, _callback):
        # Never resolves, so the callback that announces a free lane never fires.
        return None


class _FakePool:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, record, cancellation):
        self.submitted.append(record.task_id)
        return _PendingFuture()

    def shutdown(self, wait=False, cancel_futures=True):
        return None


def _retired_payload(task_type: str, task_id: str) -> dict:
    scope = TEST_SCOPE.to_dict()
    if task_type == "memory_distillation":
        return {"id": task_id, "type": task_type, "task_text": "task", "response_text": "x", "scope": scope}
    return {"id": task_id, "type": task_type, "expected_cursor": "", "scope": scope}


@pytest.fixture
def lane(tmp_path):
    """A store, a queue, and an executor over them, with a pool that never runs work."""
    from ouroboros.agent_runtime.executor import TaskExecutor
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.queue import TaskQueue

    store = runtime_persistence.initialize()
    task_queue = TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=10,
        hard_timeout=20,
        audit_event_repository=sql_audit_event_repository(),
    )
    executor = TaskExecutor(
        startup_usage_repository=sql_startup_usage_repository(),
        session_repository=sql_owner_session_repository(),
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_queue=task_queue,
        tasks_repo=store.tasks_repo,
        events=RecordingAgentEvents(),
        skill_snapshot=make_empty_skill_snapshot(),
        skill_availability=make_skill_availability(),
        extension_runtime=make_empty_extension_runtime(),
        runtime_id="test-runtime",
        work_signal=NullWorkSignal(),
        execution_memory_repository=sql_execution_memory_repository(),
        tool_confirmation_reader=sql_tool_confirmation_reader(),
        audit_event_repository=sql_audit_event_repository(),
    )
    return MagicMock(store=store, task_queue=task_queue, executor=executor, drive_root=tmp_path)


@pytest.mark.parametrize("task_type", _RETIRED)
@pytest.mark.parametrize("status", ["queued", "scheduled", "interrupted", "running"])
def test_a_leftover_retired_row_is_cancelled_at_executor_startup(tmp_path, task_type, status):
    from ouroboros.agent_runtime.executor import TaskExecutor
    from ouroboros.storage.runtime_models import TaskRecord
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.queue import TaskQueue

    store = runtime_persistence.initialize()
    store.tasks_repo.upsert_task(
        TaskRecord(
            owner_id=TEST_SCOPE.owner_id,
            agent_id=TEST_SCOPE.agent_id,
            task_id="leftover",
            task_type=task_type,
            status=status,
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    task_queue = TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=10,
        hard_timeout=20,
        audit_event_repository=sql_audit_event_repository(),
    )

    TaskExecutor(
        startup_usage_repository=sql_startup_usage_repository(),
        session_repository=sql_owner_session_repository(),
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_queue=task_queue,
        tasks_repo=store.tasks_repo,
        events=RecordingAgentEvents(),
        skill_snapshot=make_empty_skill_snapshot(),
        skill_availability=make_skill_availability(),
        extension_runtime=make_empty_extension_runtime(),
        runtime_id="test-runtime",
        work_signal=NullWorkSignal(),
        execution_memory_repository=sql_execution_memory_repository(),
        tool_confirmation_reader=sql_tool_confirmation_reader(),
        audit_event_repository=sql_audit_event_repository(),
    )

    record = store.tasks_repo.get_task("leftover")
    assert record is not None
    assert record.status == "cancelled"
    assert f"{task_type} is retired" in str(record.result or "")


@pytest.mark.parametrize("task_type", _RETIRED)
def test_a_retired_row_is_never_claimed_by_any_worker(lane, task_type):
    """The foreground worker is the only claimant left, and it must not take these.

    ``to_agent_input()`` on such a row returns the memory payload, with no
    ``text`` at all, so a foreground claim would feed that into the agent loop.
    """
    lane.task_queue.enqueue_dict(_retired_payload(task_type, "mem1"))
    lane.task_queue.enqueue_dict(
        {"id": "fg1", "type": "task", "text": "hello", "chat_id": 1, "scope": TEST_SCOPE.to_dict()}
    )
    lane.executor._pool = _FakePool()

    lane.executor.assign_next()

    assert lane.executor._slots[0].busy_task_id == "fg1"
    assert lane.store.tasks_repo.get_task("mem1").status == "queued"  # untouched, never claimed


@pytest.mark.parametrize("task_type", _RETIRED)
def test_a_retired_type_opens_no_slot_of_its_own(lane, task_type):
    lane.task_queue.enqueue_dict(_retired_payload(task_type, "mem1"))
    lane.executor._pool = _FakePool()

    lane.executor.assign_next()

    assert list(lane.executor.worker_slots_dict()) == [0]


def test_the_executor_runs_one_worker(tmp_path):
    """The second slot existed for the memory lane; there is no memory lane."""
    from ouroboros.agent_runtime.executor import TaskExecutor
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.queue import TaskQueue

    store = runtime_persistence.initialize()
    task_queue = TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=10,
        hard_timeout=20,
        audit_event_repository=sql_audit_event_repository(),
    )
    executor = TaskExecutor(
        startup_usage_repository=sql_startup_usage_repository(),
        session_repository=sql_owner_session_repository(),
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_queue=task_queue,
        tasks_repo=store.tasks_repo,
        events=RecordingAgentEvents(),
        skill_snapshot=make_empty_skill_snapshot(),
        skill_availability=make_skill_availability(),
        extension_runtime=make_empty_extension_runtime(),
        runtime_id="test-runtime",
        work_signal=NullWorkSignal(),
        execution_memory_repository=sql_execution_memory_repository(),
        tool_confirmation_reader=sql_tool_confirmation_reader(),
        audit_event_repository=sql_audit_event_repository(),
    )
    executor.ensure_started()
    try:
        assert list(executor.worker_slots_dict()) == [0]
        assert executor._pool._max_workers == 1
    finally:
        executor.shutdown()
