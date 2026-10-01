"""Tests for zombie task prevention with store-backed queue state."""

from __future__ import annotations

from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_store import init_runtime_store
from ouroboros.storage.task_results import STATUS_CANCELLED, STATUS_COMPLETED, STATUS_FAILED, load_task_result


def test_write_failure_result_creates_store_record(tmp_path) -> None:
    import supervisor.workers as workers
    from supervisor.runtime_context import reset_runtime_for_tests

    try:
        workers.init(tmp_path, tmp_path, 1, 10, 20, 10)
        workers._write_failure_result("abc123")
    finally:
        reset_runtime_for_tests()

    loaded = load_task_result(tmp_path, "abc123")
    assert loaded is not None
    assert loaded["task_id"] == "abc123"
    assert loaded["status"] == STATUS_FAILED
    assert isinstance(loaded["result"], str) and len(loaded["result"]) > 0


def test_write_failure_result_does_not_overwrite_terminal(tmp_path) -> None:
    import supervisor.workers as workers
    from ouroboros.storage.task_results import write_task_result
    from supervisor.runtime_context import reset_runtime_for_tests

    try:
        workers.init(tmp_path, tmp_path, 1, 10, 20, 10)
        write_task_result(tmp_path, "xyz789", STATUS_COMPLETED, result="Success!")
        workers._write_failure_result("xyz789")
    finally:
        reset_runtime_for_tests()

    loaded = load_task_result(tmp_path, "xyz789")
    assert loaded is not None
    assert loaded["status"] == STATUS_COMPLETED
    assert loaded["result"] == "Success!"


def test_a_killed_record_is_not_revived_by_the_start_of_run_write(tmp_path) -> None:
    """A body that starts after the sweep killed its task must not overwrite the cause of death."""
    from ouroboros.storage.task_results import STATUS_RUNNING, write_task_result

    write_task_result(tmp_path, "killed", STATUS_FAILED, result="Task killed by hard timeout after 1800s.")

    write_task_result(tmp_path, "killed", STATUS_RUNNING, result="Task is running.")

    loaded = load_task_result(tmp_path, "killed")
    assert loaded is not None
    assert loaded["status"] == STATUS_FAILED
    assert loaded["result"] == "Task killed by hard timeout after 1800s."


def test_write_failure_result_none_task_id(tmp_path) -> None:
    import supervisor.workers as workers
    from supervisor.runtime_context import reset_runtime_for_tests

    try:
        workers.init(tmp_path, tmp_path, 1, 10, 20, 10)
        workers._write_failure_result(None)
        workers._write_failure_result("")
    finally:
        reset_runtime_for_tests()

    store = init_runtime_store(tmp_path)
    assert store.list_tasks() == []


def test_drain_all_pending_returns_and_clears_store(tmp_path) -> None:
    from supervisor.queue import TaskQueue
    from supervisor.state import get_state_repository

    store = init_runtime_store(tmp_path)
    task_queue = TaskQueue(
        store,
        tmp_path,
        soft_timeout=10,
        hard_timeout=20,
        state_repo=get_state_repository(tmp_path),
    )
    task_queue.enqueue_dict({"id": "t1", "type": "task"})
    task_queue.enqueue_dict({"id": "t2", "type": "evolution"})
    drained = [dict(record.input) for record in task_queue.drain_all_pending()]

    assert len(drained) == 2
    assert task_queue.list_pending_tasks() == []


def test_kill_workers_marks_running_and_pending_terminal(tmp_path) -> None:
    import supervisor.workers as workers
    from supervisor.runtime_context import reset_runtime_for_tests
    from supervisor.testing import bootstrap_supervisor_for_tests

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    store = init_runtime_store(tmp_path)

    running = store.create_task(TaskRequest(id="run1", type="task", text="run"), status="running")
    store.create_task(TaskRequest(id="run2", type="task", text="run"), status="running")
    store.create_task(TaskRequest(id="pend1", type="evolution", text="pend"), status="queued")
    store.create_task(TaskRequest(id="pend2", type="task", text="pend"), status="queued")

    runtime.executor.ensure_started()
    slot = runtime.executor.get_slot(0)
    assert slot is not None
    slot.busy_task_id = running.task_id

    try:
        workers.kill_workers()
    finally:
        reset_runtime_for_tests()

    for tid in ("run1", "run2", "pend1", "pend2"):
        loaded = load_task_result(tmp_path, tid)
        assert loaded is not None, f"Missing failure result for {tid}"
        assert loaded["status"] == STATUS_FAILED
        assert loaded["task_id"] == tid


def test_kill_workers_can_record_owner_restart_cancellation(tmp_path) -> None:
    import supervisor.workers as workers
    from supervisor.runtime_context import reset_runtime_for_tests
    from supervisor.testing import bootstrap_supervisor_for_tests

    bootstrap_supervisor_for_tests(tmp_path)
    store = init_runtime_store(tmp_path)
    store.create_task(TaskRequest(id="run1", type="task", text="run"), status="running")
    store.create_task(TaskRequest(id="pend1", type="task", text="pend"), status="queued")

    try:
        workers.kill_workers(
            result_status="cancelled",
            result_reason="Owner restart stopped this task before process restart.",
        )
    finally:
        reset_runtime_for_tests()

    for tid in ("run1", "pend1"):
        loaded = load_task_result(tmp_path, tid)
        assert loaded is not None
        assert loaded["status"] == STATUS_CANCELLED
        assert loaded["result"] == "Owner restart stopped this task before process restart."
