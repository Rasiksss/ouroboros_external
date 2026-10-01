from __future__ import annotations


def test_runtime_store_task_lifecycle(tmp_path) -> None:
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_store import init_runtime_store

    store = init_runtime_store(tmp_path)
    store.create_task(
        TaskRequest(
            id="task1",
            type="task",
            chat_id=42,
            text="Do it",
            priority=2,
            source="dialogs",
        )
    )

    claimed = store.claim_next_task(worker_id=0)
    assert claimed is not None
    assert claimed.task_id == "task1"
    assert claimed.status == "running"
    assert claimed.input["text"] == "Do it"

    store.heartbeat_task("task1", phase="running")
    completed = store.complete_task("task1", result="done", duration_sec=0.1)

    assert completed is not None
    assert completed.status == "completed"
    assert completed.result == "done"
    assert completed.metadata["duration_sec"] == 0.1


def test_task_results_facade_writes_sqlite_only(tmp_path) -> None:
    from ouroboros.storage.runtime_store import init_runtime_store
    from ouroboros.storage.task_results import STATUS_COMPLETED, load_task_result, write_task_result

    store = init_runtime_store(tmp_path)
    write_task_result(tmp_path, "task2", STATUS_COMPLETED, result="ok")

    record = store.get_task("task2")
    assert record is not None
    assert record.status == STATUS_COMPLETED
    assert record.result == "ok"
    assert not (tmp_path / "task_results" / "task2.json").exists()

    loaded = load_task_result(tmp_path, "task2")
    assert loaded is not None
    assert loaded["status"] == STATUS_COMPLETED
    assert loaded["result"] == "ok"


def _claimed_task(tmp_path, task_id: str = "task1"):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_store import init_runtime_store

    store = init_runtime_store(tmp_path)
    store.create_task(TaskRequest(id=task_id, type="task", text="run"))
    claimed = store.claim_next_task(worker_id=0)
    assert claimed is not None and claimed.task_id == task_id
    return store


def test_heartbeat_cannot_resurrect_a_failed_task(tmp_path) -> None:
    store = _claimed_task(tmp_path)
    store.fail_task("task1", result="Task killed by hard timeout after 1800s.")
    failed_at = store.get_task("task1").heartbeat_at

    store.heartbeat_task("task1", phase="running")

    record = store.get_task("task1")
    assert record.status == "failed"
    assert record.heartbeat_at == failed_at


def test_late_completion_cannot_overwrite_a_failed_task(tmp_path) -> None:
    store = _claimed_task(tmp_path)
    store.fail_task("task1", result="Task killed by hard timeout after 1800s.")

    store.complete_task("task1", result="Finished, eventually.")

    record = store.get_task("task1")
    assert record.status == "failed"
    assert record.result == "Task killed by hard timeout after 1800s."


def test_heartbeat_on_a_running_task_advances_heartbeat_at(tmp_path) -> None:
    store = _claimed_task(tmp_path)
    claimed_at = store.get_task("task1").heartbeat_at

    store.heartbeat_task("task1", phase="tool_call")

    record = store.get_task("task1")
    assert record.status == "running"
    assert record.heartbeat_at > claimed_at
    assert record.metadata["heartbeat_phase"] == "tool_call"


def test_heartbeat_on_a_queued_task_is_a_no_op(tmp_path) -> None:
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_store import init_runtime_store

    store = init_runtime_store(tmp_path)
    queued = store.create_task(TaskRequest(id="task1", type="task", text="run"))

    store.heartbeat_task("task1", phase="running")

    record = store.get_task("task1")
    assert record.status == "queued"
    assert record.heartbeat_at == queued.heartbeat_at
    assert record.started_at == queued.started_at


def test_rewriting_the_same_terminal_status_still_lands(tmp_path) -> None:
    store = _claimed_task(tmp_path)
    store.fail_task("task1", result="first")

    store.fail_task("task1", result="second", total_rounds=5)

    record = store.get_task("task1")
    assert record.result == "second"
    assert record.metadata["total_rounds"] == 5
