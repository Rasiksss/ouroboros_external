"""Several runs at once: separate agents, several slots, and one stuck body stopping none.

The serialisation had three layers — one slot, one cached agent, one lock around the body —
and they only come off together. What is pinned here is the result: an instance runs the
number of tasks it is configured for, and nothing carrying a run's state is shared.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future

from new_core.coordination.inprocess import NullWorkSignal
from ouroboros.agent_runtime.executor import TaskExecutor, WorkerSlot
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.queue import TaskQueue
from tests.helpers.agent_events import RecordingAgentEvents
from tests.helpers.agent_owner_session import sql_owner_session_repository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import (
    TEST_SCOPE,
    make_empty_extension_runtime,
    make_empty_skill_snapshot,
    make_skill_availability,
    scope_with,
)
from tests.helpers.startup_usage import sql_startup_usage_repository
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


def _executor(tmp_path, store) -> TaskExecutor:
    return TaskExecutor(
        startup_usage_repository=sql_startup_usage_repository(),
        session_repository=sql_owner_session_repository(),
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_queue=TaskQueue(
            store.tasks_repo,
            tmp_path,
            work_signal=NullWorkSignal(),
            soft_timeout=1,
            hard_timeout=60,
            audit_event_repository=sql_audit_event_repository(),
        ),
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


def _queue_for(executor: TaskExecutor, task_id: str, owner: str) -> None:
    scope = scope_with(TEST_SCOPE, owner_id=owner)
    executor.task_queue.enqueue_dict(
        {"id": task_id, "type": "task", "text": "go", "chat_id": int(scope.chat_id), "scope": scope.to_dict()}
    )


def _slots(monkeypatch, count: int) -> None:
    monkeypatch.setattr("ouroboros.agent_runtime.executor._configured_worker_slots", lambda: count)


# --- agent per run ----------------------------------------------------------


def test_two_runs_hold_different_agents(tmp_path) -> None:
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)

    first = executor.new_agent()
    second = executor.new_agent()

    assert first is not second
    assert first._pending_events is not second._pending_events
    assert first._task_usage is not second._task_usage
    assert first._run_state is None and second._run_state is None


def test_neither_agent_observes_the_others_run(tmp_path) -> None:
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    first = executor.new_agent()
    second = executor.new_agent()

    first._current_task_id = "t-first"
    first._pending_events.append({"type": "progress"})
    first._task_usage["prompt_tokens"] = 10

    assert second._current_task_id is None
    assert second._pending_events == []
    assert second._task_usage == {}


def test_the_startup_checks_run_once_however_many_agents_are_built(tmp_path, monkeypatch) -> None:
    """The flag and the checks are under one lock, so a second agent never starts mid-check."""
    import ouroboros.agent.core as core

    monkeypatch.setattr(core, "_worker_boot_logged", False)
    verified: list[int] = []
    monkeypatch.setattr(
        core,
        "verify_system_state",
        lambda _env, *, startup_usage_repository, audit_event_repository: verified.append(1),
    )
    monkeypatch.setattr(core, "inject_crash_report", lambda _env, *, audit_event_repository: None)
    monkeypatch.setattr(core, "record_event", lambda *_args, **_kwargs: None)
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)

    built: list = []
    threads = [threading.Thread(target=lambda: built.append(executor.new_agent())) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(built) == 4
    assert verified == [1]


# --- the slots --------------------------------------------------------------


def test_the_configured_number_of_slots_is_what_runs(tmp_path, monkeypatch) -> None:
    _slots(monkeypatch, 3)
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)

    executor.ensure_started()

    assert sorted(slot.worker_id for slot in executor.all_slots()) == [0, 1, 2]


def test_assign_next_fills_every_free_slot(tmp_path, monkeypatch) -> None:
    _slots(monkeypatch, 3)
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    started: list[str] = []
    executor._run_task = lambda record, _cancellation: started.append(record.task_id)  # type: ignore[method-assign]
    for index, owner in enumerate(("alice", "bob", "carol")):
        _queue_for(executor, f"t{index}", owner)

    executor.assign_next()
    for slot in executor.all_slots():
        if slot.future is not None:
            slot.future.result(timeout=5)

    assert sorted(started) == ["t0", "t1", "t2"]


def test_one_stuck_slot_does_not_stop_the_others(tmp_path, monkeypatch) -> None:
    _slots(monkeypatch, 3)
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    # Slot 0 holds a body that has not returned; the future never resolves.
    executor._slots[0] = WorkerSlot(worker_id=0, busy_task_id="stuck", future=Future())
    started: list[str] = []
    executor._run_task = lambda record, _cancellation: started.append(record.task_id)  # type: ignore[method-assign]
    _queue_for(executor, "t1", "bob")
    _queue_for(executor, "t2", "carol")

    executor.assign_next()
    for slot in executor.all_slots():
        if slot.future is not None and slot.busy_task_id != "stuck":
            slot.future.result(timeout=5)

    assert sorted(started) == ["t1", "t2"]
    assert executor._slots[0].busy_task_id == "stuck"


def test_one_owners_second_task_does_not_take_a_second_slot(tmp_path, monkeypatch) -> None:
    """The rule lives in the claim, so the executor offering three slots changes nothing."""
    _slots(monkeypatch, 3)
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    started: list[str] = []
    executor._run_task = lambda record, _cancellation: started.append(record.task_id)  # type: ignore[method-assign]
    _queue_for(executor, "first", "alice")
    _queue_for(executor, "second", "alice")

    executor.assign_next()
    for slot in executor.all_slots():
        if slot.future is not None:
            slot.future.result(timeout=5)

    assert started == ["first"]


def test_a_real_body_runs_on_every_slot_at_once(tmp_path, monkeypatch) -> None:
    """Not a claim count: three bodies are inside their agents at the same moment."""
    _slots(monkeypatch, 3)
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    inside = threading.Barrier(3, timeout=10)

    class _Agent:
        def handle_task(self, task, *, cancellation):
            inside.wait()
            return []

    executor.new_agent = lambda: _Agent()  # type: ignore[method-assign]
    for index, owner in enumerate(("alice", "bob", "carol")):
        _queue_for(executor, f"t{index}", owner)

    executor.assign_next()
    deadline = time.monotonic() + 10
    for slot in executor.all_slots():
        assert slot.future is not None
        slot.future.result(timeout=max(0.1, deadline - time.monotonic()))
