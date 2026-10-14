"""A killed or cancelled task stops its own body, and a late answer says it is late.

`future.cancel()` returns False for a future that has already started, so before this the
supervisor freed the slot, the record went `failed`, and the body kept running — holding
the agent lock, so every task claimed after it timed out too. These tests pin the two
halves of the fix: the stop signal the body observes, and the slot that stays claimed
until it actually returns.

The sweeps no longer reach into a lane at all. They close the record, and the body sees its
own closed record through ``StoreCancellation`` — which is why the stop still lands on a run
the sweeping component is not holding, and could not hold once the two are separate
processes.
"""

from __future__ import annotations

import datetime
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from new_core.coordination.inprocess import NullWorkSignal
from new_core.coordination.interfaces import TaskStopNotice
from ouroboros.agent.task_pipeline import emit_task_results
from ouroboros.agent_events.interfaces import OutboundText, TaskFinished, TaskMetrics
from ouroboros.agent_runtime.cancellation import EventCancellation, StoreCancellation
from ouroboros.agent_runtime.executor import _STOP_GRACE_SECONDS, TaskExecutor, WorkerSlot
from ouroboros.storage.runtime_models import TaskRequest
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
)
from tests.helpers.startup_usage import sql_startup_usage_repository
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


def _iso_ago(seconds: float) -> str:
    return (datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=seconds)).isoformat()


def _queue(tmp_path, store):
    return TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=1,
        hard_timeout=60,
        audit_event_repository=sql_audit_event_repository(),
    )


def _running_task(store, task_id: str, *, age_sec: float = 120.0, task_type: str = "task"):
    """A record as a claim leaves it, aged backwards.

    Claimed rather than written straight to `running`: `runtime_id` is set by the claim and
    by nothing else, and the paths under test read it to decide who holds the run.
    """
    store.tasks_repo.create_task(
        TaskRequest(
            id=task_id, type=task_type, text="hangs forever", chat_id=42, source="dialogs", scope=TEST_SCOPE.to_dict()
        )
    )
    record = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="test-runtime")
    assert record is not None and record.task_id == task_id
    record.started_at = _iso_ago(age_sec)
    record.heartbeat_at = record.started_at
    return store.tasks_repo.upsert_task(record)


def _record_signal(store, task_id: str = "hung") -> StoreCancellation:
    """The half of a run's stop signal that crosses instances: its own record."""
    return StoreCancellation(
        store.tasks_repo,
        task_id=task_id,
        scope=TEST_SCOPE,
        poll_interval_sec=0.0,
    )


def _claimed_slot(task_id: str = "hung", worker_id: int = 0) -> WorkerSlot:
    """A slot as `_assign_slot` leaves it: a task id, a running future, a fresh signal."""
    return WorkerSlot(
        worker_id=worker_id,
        busy_task_id=task_id,
        future=Future(),  # never resolved: the body is still running
    )


# --- the signal, and who sets it -------------------------------------------


def test_the_signal_reports_a_stop_only_once_requested() -> None:
    signal = EventCancellation()
    assert signal.is_stopped() is False

    signal.request_stop()
    signal.request_stop()

    assert signal.is_stopped() is True


def test_hard_timeout_stops_the_body_through_its_own_record(tmp_path) -> None:
    """The sweep holds no lane and needs none: the record it closes is the stop."""
    store = runtime_persistence.initialize()
    _running_task(store, "hung")
    signal = _record_signal(store)
    assert signal.is_stopped() is False

    _queue(tmp_path, store).enforce_task_timeouts()

    assert store.tasks_repo.get_task("hung").status == "failed"
    assert signal.is_stopped() is True


def _recording_slot(store, task_id: str = "hung"):
    """A claimed slot that notes the stored status of its task each time it is asked to stop."""
    status_at_stop: list[str] = []

    class _RecordingSlot(WorkerSlot):
        def request_stop(self) -> None:
            status_at_stop.append(store.tasks_repo.get_task(task_id).status)
            super().request_stop()

    return _RecordingSlot(worker_id=0, busy_task_id=task_id, future=Future()), status_at_stop


def test_shutdown_sets_the_stop_only_after_the_records_are_terminal(tmp_path) -> None:
    store = runtime_persistence.initialize()
    _running_task(store, "hung")
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    slot, status_at_stop = _recording_slot(store)
    executor._slots[0] = slot

    executor.shutdown()

    # A release rather than a failure now, but still terminal before the stop goes out.
    assert status_at_stop == ["cancelled"]


def test_cancelling_a_running_task_stops_the_body_through_its_own_record(tmp_path) -> None:
    """The record decides, and closing it is the whole of the cancellation."""
    store = runtime_persistence.initialize()
    _running_task(store, "hung", age_sec=1.0)
    signal = _record_signal(store)

    assert _queue(tmp_path, store).cancel_task_by_id("hung", scope=TEST_SCOPE) is True

    assert store.tasks_repo.get_task("hung").status == "cancelled"
    assert signal.is_stopped() is True


def test_a_second_stop_request_does_not_push_out_the_grace_period() -> None:
    slot = _claimed_slot()
    slot.request_stop()
    first = slot.stop_requested_at
    time.sleep(0.01)
    slot.request_stop()
    assert slot.stop_requested_at == first


def test_a_notice_sets_only_the_matching_runtime_task_signal(tmp_path) -> None:
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    first = _claimed_slot(task_id="first", worker_id=0)
    second = _claimed_slot(task_id="second", worker_id=1)
    executor._slots = {0: first, 1: second}

    executor.handle_stop_notice(TaskStopNotice(runtime_id="test-runtime", task_ids=("first",)))

    assert first.cancellation.is_stopped() is True
    assert second.cancellation.is_stopped() is False


def test_a_notice_for_another_runtime_changes_no_signal(tmp_path) -> None:
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    slot = _claimed_slot(task_id="first", worker_id=0)
    executor._slots = {0: slot}

    executor.handle_stop_notice(TaskStopNotice(runtime_id="somewhere-else", task_ids=("first",)))

    assert slot.cancellation.is_stopped() is False


# --- the body observes it ---------------------------------------------------


class _LoopingAgent:
    """A body that runs rounds until it is asked to stop, like the real loop."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.rounds = 0
        self.saw_signal = False

    def handle_task(self, task, *, cancellation):
        self.entered.set()
        while not cancellation.is_stopped():
            self.rounds += 1
            time.sleep(0.005)
        self.saw_signal = True
        return []


def _executor(tmp_path, store) -> TaskExecutor:
    return TaskExecutor(
        startup_usage_repository=sql_startup_usage_repository(),
        session_repository=sql_owner_session_repository(),
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_queue=_queue(tmp_path, store),
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


def test_a_stopped_body_returns_and_blocks_nothing(tmp_path) -> None:
    """There is no shared lock left to release: the body held only its own agent."""
    store = runtime_persistence.initialize()
    record = _running_task(store, "hung")
    executor = _executor(tmp_path, store)
    agent = _LoopingAgent()
    executor.new_agent = lambda: agent  # type: ignore[method-assign]
    slot = _claimed_slot()

    thread = threading.Thread(target=executor._run_task, args=(record, slot.cancellation), daemon=True)
    thread.start()
    assert agent.entered.wait(timeout=5)

    slot.request_stop()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert agent.saw_signal
    assert agent.rounds >= 1


def test_every_run_is_handed_an_agent_of_its_own(tmp_path) -> None:
    """Two runs never meet inside one agent, which is what the lock stood in for."""
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)

    first = executor.new_agent()
    second = executor.new_agent()

    assert first is not second


def test_the_body_observes_the_signal_it_was_handed(tmp_path) -> None:
    store = runtime_persistence.initialize()
    record = _running_task(store, "plain")
    executor = _executor(tmp_path, store)
    slot = _claimed_slot(task_id="plain")
    seen: list = []

    class _Agent:
        def handle_task(self, task, *, cancellation):
            seen.append(cancellation)
            return []

    executor.new_agent = lambda: _Agent()  # type: ignore[method-assign]
    executor._run_task(record, slot.cancellation)

    assert seen == [slot.cancellation]


def test_a_body_is_handed_the_signal_of_the_claim_that_started_it(tmp_path) -> None:
    """Read at submit time, not in the worker thread: by then the slot may have moved on.

    The body is handed a composition — the claim's own event, and the record behind it —
    so what is pinned here is that the event inside it is the claim's, not the slot's next.
    """
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    executor.task_queue.enqueue_dict(
        {"id": "first", "type": "task", "text": "go", "chat_id": 42, "scope": TEST_SCOPE.to_dict()}
    )
    handed: list = []
    executor._run_task = lambda _record, cancellation: handed.append(cancellation)  # type: ignore[method-assign]
    slot = executor._slots[0]

    assert executor._assign_slot(slot) is not None
    claimed_signal = slot.cancellation
    slot.future.result(timeout=5)
    slot.clear_task()

    assert len(handed) == 1
    assert handed[0].is_stopped() is False
    slot.cancellation.request_stop()
    assert handed[0].is_stopped() is False
    claimed_signal.request_stop()
    assert handed[0].is_stopped() is True


# --- the slot stays claimed, and abandonment is visible ---------------------


def test_no_task_is_claimed_while_a_stopping_body_still_holds_the_slot(tmp_path) -> None:
    store = runtime_persistence.initialize()
    _running_task(store, "hung")
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    queued = executor.task_queue.enqueue_dict(
        {"id": "next", "type": "task", "text": "waiting", "chat_id": 42, "scope": TEST_SCOPE.to_dict()}
    )
    slot = _claimed_slot()
    executor._slots[0] = slot
    slot.request_stop()

    assert executor._assign_slot(slot) is None
    assert store.tasks_repo.get_task(queued.task_id).status == "queued"
    assert slot.busy_task_id == "hung"


def test_a_body_that_stops_in_time_frees_its_slot_without_an_abandonment(tmp_path, monkeypatch) -> None:
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    events: list = []
    monkeypatch.setattr(
        "ouroboros.agent_runtime.executor.record_event",
        lambda _cat, name, payload, **_kwargs: events.append((name, payload)),
    )
    slot = _claimed_slot()
    executor._slots[0] = slot
    slot.request_stop()
    slot.future.set_result(None)

    executor.ensure_healthy()

    assert slot.busy_task_id is None
    assert slot.stop_requested_at is None
    # A fresh signal, so the next claim does not inherit this task's stop.
    assert slot.cancellation.is_stopped() is False
    assert [name for name, _ in events if name == "task_abandoned"] == []


def test_a_body_that_ignores_the_signal_is_released_and_reported(tmp_path, monkeypatch) -> None:
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    events: list = []
    monkeypatch.setattr(
        "ouroboros.agent_runtime.executor.record_event",
        lambda _cat, name, payload, **_kwargs: events.append((name, payload)),
    )
    slot = _claimed_slot()
    executor._slots[0] = slot
    slot.request_stop()
    slot.stop_requested_at = time.monotonic() - _STOP_GRACE_SECONDS - 1

    executor.ensure_healthy()

    assert slot.busy_task_id is None
    assert slot.future is None
    abandoned = [payload for name, payload in events if name == "task_abandoned"]
    assert len(abandoned) == 1
    assert abandoned[0]["task_id"] == "hung"
    assert abandoned[0]["elapsed_sec"] > _STOP_GRACE_SECONDS


def test_an_abandoned_slot_replaces_nothing_shared(tmp_path, monkeypatch) -> None:
    """The leaked body holds only its own objects, so the slot has nothing to hand over."""
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    monkeypatch.setattr("ouroboros.agent_runtime.executor.record_event", lambda *_args, **_kwargs: None)
    slot = _claimed_slot()
    executor._slots[0] = slot
    slot.request_stop()
    slot.stop_requested_at = time.monotonic() - _STOP_GRACE_SECONDS - 1
    factory_before = executor._agent_factory

    executor.ensure_healthy()

    assert slot.busy_task_id is None
    assert executor._agent_factory is factory_before
    assert not hasattr(executor, "_agent_lock")


def test_an_abandonment_hands_the_remaining_slots_a_fresh_pool(tmp_path, monkeypatch) -> None:
    """The leaked thread keeps a worker of the old pool, so the next claim must not queue behind it."""
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    monkeypatch.setattr("ouroboros.agent_runtime.executor.record_event", lambda *_args, **_kwargs: None)
    executor.ensure_started()
    leaked_pool = executor._pool
    slot = _claimed_slot()
    executor._slots[0] = slot
    slot.request_stop()
    slot.stop_requested_at = time.monotonic() - _STOP_GRACE_SECONDS - 1

    executor.ensure_healthy()

    assert executor._pool is not leaked_pool
    # A task claimed after the leak runs, instead of waiting for a worker that never returns.
    executor.task_queue.enqueue_dict(
        {"id": "next", "type": "task", "text": "go", "chat_id": 42, "scope": TEST_SCOPE.to_dict()}
    )
    ran: list = []
    executor._run_task = lambda _record, _cancellation: ran.append(_record.task_id)  # type: ignore[method-assign]
    assert executor._assign_slot(executor._slots[0]) is not None
    executor._slots[0].future.result(timeout=5)
    assert ran == ["next"]


def test_a_slot_that_was_never_stopped_is_left_alone(tmp_path, monkeypatch) -> None:
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    events: list = []
    monkeypatch.setattr(
        "ouroboros.agent_runtime.executor.record_event",
        lambda _cat, name, payload, **_kwargs: events.append((name, payload)),
    )
    slot = _claimed_slot()
    executor._slots[0] = slot

    executor.ensure_healthy()

    assert slot.busy_task_id == "hung"
    assert events == []


# --- shutdown ---------------------------------------------------------------


def test_shutdown_releases_claim_committed_before_slot_registration(tmp_path, monkeypatch):
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="claim-gap", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    claim = executor.task_queue.claim_next
    committed, resume = threading.Event(), threading.Event()
    started = []

    def pause_after_commit(*args, **kwargs):
        record = claim(*args, **kwargs)
        committed.set()
        assert resume.wait(10)
        return record

    monkeypatch.setattr(executor.task_queue, "claim_next", pause_after_commit)
    monkeypatch.setattr(executor, "_run_task", lambda record, stop: started.append(record.task_id))
    claiming = threading.Thread(target=executor.assign_next)
    claiming.start()
    try:
        assert committed.wait(5)
        executor.drain(deadline_sec=5.0)
    finally:
        resume.set()
        claiming.join(5)
    assert not claiming.is_alive() and not started
    predecessor = store.tasks_repo.get_task("claim-gap")
    successor = store.tasks_repo.get_current_job("claim-gap", scope=TEST_SCOPE)
    assert predecessor.status == "cancelled" and predecessor.error_code == "instance_released"
    assert successor.task_id != predecessor.task_id and successor.status == "queued"
    assert successor.attempt == predecessor.attempt == 1
    assert not executor.held_task_ids()


def test_executor_shutdown_stops_every_running_task_through_its_own_signal(tmp_path) -> None:
    store = runtime_persistence.initialize()
    _running_task(store, "hung")
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    slot = _claimed_slot()
    executor._slots[0] = slot
    signal = slot.cancellation
    assert signal.is_stopped() is False

    executor.shutdown()

    # The body holds the signal it was handed, so a released slot still stops it.
    assert signal.is_stopped() is True


def test_shutdown_cleans_up_local_state_when_releasing_own_runs_fails(tmp_path, monkeypatch) -> None:
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    slot = _claimed_slot()
    executor._slots[0] = slot
    signal = slot.cancellation
    pool = executor._pool
    assert pool is not None
    release_error = RuntimeError("release failed")
    stopped_at_release: list[bool] = []

    def _fail_release(*, runtime_id: str, claims: dict[str, str]) -> None:
        assert runtime_id == "test-runtime"
        stopped_at_release.append(signal.is_stopped())
        raise release_error

    monkeypatch.setattr(executor.task_queue, "release_own_runs", _fail_release)
    try:
        with pytest.raises(RuntimeError) as raised:
            executor.shutdown()

        assert raised.value is release_error
        assert stopped_at_release == [False]
        assert signal.is_stopped() is True
        assert slot.busy_task_id is None
        assert slot.future is None
        assert executor._pool is None
        with pytest.raises(RuntimeError):
            pool.submit(lambda: None)
    finally:
        if executor._pool is not None:
            executor._pool.shutdown(wait=False, cancel_futures=True)


# --- the late-result policy -------------------------------------------------


@pytest.fixture()
def killed_task(tmp_path):
    """A record already failed by the hard timeout, as a late body would find it."""
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="killed", type="task", text="do it", chat_id=7, source="dialogs", scope=TEST_SCOPE.to_dict()),
        status="running",
    )
    store.tasks_repo.fail_task("killed", result="Task killed by hard timeout after 1800s.")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    return store


def _emit(tmp_path, task_id, text, *, has_answer=True, stopped_externally=True):
    pending: list = []
    emit_task_results(
        SimpleNamespace(drive_root=tmp_path),
        object(),
        pending,
        {"id": task_id, "type": "task", "chat_id": 7, "text": "do it"},
        text,
        {"rounds": 3},
        {"tool_calls": []},
        0.0,
        tmp_path / "logs",
        scope=TEST_SCOPE,
        has_answer=has_answer,
        stopped_externally=stopped_externally,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    return pending


def _of_type(pending, kind):
    return [event for event in pending if isinstance(event, kind)]


def test_a_late_answer_reaches_the_owner_labelled_as_late(tmp_path, killed_task) -> None:
    killed_at = killed_task.tasks_repo.get_task("killed").finished_at
    pending = _emit(tmp_path, "killed", "here is the answer")

    sent = _of_type(pending, OutboundText)
    assert len(sent) == 1
    assert sent[0].text.startswith("⏳ Late result")
    assert "hard timeout" in sent[0].text
    assert sent[0].text.endswith("here is the answer")
    # The record keeps the status, the result and the completion time that closed the task.
    record = killed_task.tasks_repo.get_task("killed")
    assert record.status == "failed"
    assert record.result == "Task killed by hard timeout after 1800s."
    assert record.finished_at == killed_at
    assert sent[0].text.startswith(f"⏳ Late result — this task had already been stopped at {killed_at}")


def test_a_cooperatively_stopped_task_sends_no_owner_message(tmp_path, killed_task) -> None:
    pending = _emit(
        tmp_path,
        "killed",
        "⚠️ Model returned an empty response. Try rephrasing your request.",
        has_answer=False,
    )

    assert _of_type(pending, OutboundText) == []
    # Everything that is not owner-facing still runs.
    assert len(_of_type(pending, TaskFinished)) == 1
    assert len(_of_type(pending, TaskMetrics)) == 1


def test_a_running_task_with_stop_intent_sends_no_empty_response(tmp_path) -> None:
    """Durable stop is intent first; its empty terminal is cancellation, not an LLM failure."""
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="stopping", type="task", text="do it", chat_id=7, source="dialogs", scope=TEST_SCOPE.to_dict()),
        status="running",
    )
    assert store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="stopping")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    pending = _emit(
        tmp_path,
        "stopping",
        "⚠️ Model returned an empty response. Try rephrasing your request.",
        has_answer=False,
    )

    assert _of_type(pending, OutboundText) == []
    assert store.tasks_repo.get_task("stopping").status == "cancelled"
    finished = _of_type(pending, TaskFinished)
    assert [(event.status, event.result) for event in finished] == [("cancelled", "Task cancelled.")]


def test_a_run_that_failed_itself_publishes_its_error_plainly(tmp_path, killed_task) -> None:
    """The crash path fails the record before publishing, so the record alone cannot tell why."""
    error = "⚠️ Error during processing: RuntimeError: boom"

    sent = _of_type(_emit(tmp_path, "killed", error, stopped_externally=False), OutboundText)

    assert len(sent) == 1
    assert sent[0].text == error


def test_an_active_record_publishes_exactly_as_before(tmp_path) -> None:
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="live", type="task", text="do it", chat_id=7, source="dialogs", scope=TEST_SCOPE.to_dict()),
        status="running",
    )
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    sent = _of_type(_emit(tmp_path, "live", "here is the answer"), OutboundText)

    assert len(sent) == 1
    assert sent[0].text == "here is the answer"


def test_emit_result_reports_a_missing_answer_to_the_pipeline(tmp_path) -> None:
    """The empty-text fallback erases the difference, so the adapter reports it separately."""
    from unittest.mock import patch

    from background_memory.interfaces import NullMemoryEventPublisher
    from ouroboros.agent.loop_adapters import EventSinkAdapter

    adapter = EventSinkAdapter(
        emit_progress=lambda _t: None,
        events=None,
        task_id="t",
        drive_logs=tmp_path,
        memory_events=NullMemoryEventPublisher(),
        scope=TEST_SCOPE,
        env=SimpleNamespace(drive_root=tmp_path),
        llm=object(),
        pending_events=[],
        task={"id": "t", "chat_id": 0},
        start_time=1.0,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )

    with patch("ouroboros.agent.task_pipeline.emit_task_results") as emit:
        adapter.emit_result(text=None, usage={}, trace={}, tool_ctx=None)
    assert emit.call_args.kwargs["has_answer"] is False

    with patch("ouroboros.agent.task_pipeline.emit_task_results") as emit:
        adapter.emit_result(text="an answer", usage={}, trace={}, tool_ctx=None)
    assert emit.call_args.kwargs["has_answer"] is True
    assert emit.call_args.kwargs["stopped_externally"] is True

    adapter.self_failed = True
    with patch("ouroboros.agent.task_pipeline.emit_task_results") as emit:
        adapter.emit_result(text="an answer", usage={}, trace={}, tool_ctx=None)
    assert emit.call_args.kwargs["stopped_externally"] is False


# --- what arms the grace clock now the sweeps reach no lane ------------------


def test_a_stop_recorded_elsewhere_arms_the_lane_s_grace_clock(tmp_path, monkeypatch) -> None:
    """Nothing pokes a lane any more, so the lane has to read the record itself.

    The regression this pins: `enforce_task_timeouts` and `cancel_task_by_id` used to call
    `slot.request_stop()`, and both moved to another tier. With no arming left outside a
    drain, `_release_if_abandoned` could never fire — a body that ignores its stop held its
    lane for the life of the process while the health surface still reported ready.
    """
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    monkeypatch.setattr("ouroboros.agent_runtime.executor.record_event", lambda *_args, **_kwargs: None)
    slot = _claimed_slot()
    slot.stop_signal = SimpleNamespace(is_stopped=lambda: True)
    executor._slots[0] = slot

    executor.ensure_healthy()

    assert slot.stop_requested_at is not None
    # Still held: the clock only started, and the grace period has not run out.
    assert slot.busy_task_id == "hung"


def test_a_lane_whose_record_carries_no_stop_is_not_armed(tmp_path, monkeypatch) -> None:
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    monkeypatch.setattr("ouroboros.agent_runtime.executor.record_event", lambda *_args, **_kwargs: None)
    slot = _claimed_slot()
    slot.stop_signal = SimpleNamespace(is_stopped=lambda: False)
    executor._slots[0] = slot

    executor.ensure_healthy()

    assert slot.stop_requested_at is None
    assert slot.busy_task_id == "hung"


def test_an_assigned_slot_keeps_the_signal_its_body_observes(tmp_path) -> None:
    """The lane and its body read the same stop, which is what makes the arming possible."""
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    executor.task_queue.enqueue_dict(
        {"id": "t1", "type": "task", "text": "go", "chat_id": 42, "scope": TEST_SCOPE.to_dict()}
    )
    executor._run_task = lambda _record, _cancellation: None  # type: ignore[method-assign]

    assert executor._assign_slot(executor._slots[0]) is not None

    assert executor._slots[0].stop_signal is not None


def test_an_abandoned_lane_closes_its_record(tmp_path, monkeypatch) -> None:
    """Otherwise the leaked body's own heartbeat keeps the row out of the stale sweep.

    The heartbeat thread stops in `handle_task`'s `finally`, which an abandoned body never
    reaches, so `heartbeat_at` never goes stale and nothing else would ever close the task —
    it would hold its owner's one running root forever.
    """
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    monkeypatch.setattr("ouroboros.agent_runtime.executor.record_event", lambda *_args, **_kwargs: None)
    store.tasks_repo.create_task(TaskRequest(id="hung", type="task", text="go", chat_id=42, scope=TEST_SCOPE.to_dict()))
    claimed = executor.task_queue.claim_next(worker_id=0, runtime_id="test-runtime")
    assert claimed is not None
    slot = _claimed_slot()
    executor._slots[0] = slot
    slot.request_stop()
    slot.stop_requested_at = time.monotonic() - _STOP_GRACE_SECONDS - 1

    executor.ensure_healthy()

    record = store.tasks_repo.get_task("hung")
    assert record is not None
    assert record.status != "running"
    assert "abandoned" in record.result.lower()
