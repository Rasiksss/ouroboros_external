from __future__ import annotations


def test_the_runtime_runs_tasks_on_threads_and_spawns_no_process(tmp_path) -> None:
    from tests.helpers.supervisor import bootstrap_agent_runtime_for_tests

    parts = bootstrap_agent_runtime_for_tests(tmp_path)
    parts.runtime._executor.ensure_started()

    total, busy = parts.runtime.worker_counts()

    assert (total, busy) == (1, 0)
    assert not any(hasattr(slot, "proc") for slot in parts.runtime._executor.all_slots())

    parts.runtime.drain(deadline_sec=0)
