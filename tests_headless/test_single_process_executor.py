from __future__ import annotations


def test_single_process_executor_does_not_spawn_processes(tmp_path) -> None:
    from supervisor import workers
    from supervisor.runtime_context import reset_runtime_for_tests

    try:
        executor = workers.init(
            repo_dir=tmp_path,
            drive_root=tmp_path,
            max_workers=5,
            soft_timeout=10,
            hard_timeout=20,
            total_budget_limit=10,
        )
        workers.spawn_workers(5)

        slots = executor.worker_slots_dict()
        assert list(slots) == [0]
        assert not hasattr(slots[0], "proc")

        workers.kill_workers()
    finally:
        reset_runtime_for_tests()


def test_handle_chat_direct_submits_durable_task(tmp_path, monkeypatch) -> None:
    from supervisor import workers
    from supervisor.runtime_context import reset_runtime_for_tests
    from supervisor.testing import bootstrap_supervisor_for_tests

    class Bridge:
        def send_message(self, *args, **kwargs):
            return True, "ok"

    try:
        runtime = bootstrap_supervisor_for_tests(tmp_path, bridge=Bridge())

        workers.handle_chat_direct(123, "hello")

        pending = runtime.task_queue.list_pending_tasks()
    finally:
        reset_runtime_for_tests()

    assert len(pending) == 1
    assert pending[0]["text"] == "hello"
    assert pending[0]["source"] == "dialogs"
