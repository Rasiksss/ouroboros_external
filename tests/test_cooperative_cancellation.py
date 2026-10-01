"""A killed or cancelled task stops its own body, and a late answer says it is late.

`future.cancel()` returns False for a future that has already started, so before this the
supervisor freed the slot, the record went `failed`, and the body kept running — holding
the agent lock, so every task claimed after it timed out too. These tests pin the two
halves of the fix: the stop signal the body observes, and the slot that stays claimed
until it actually returns.
"""

from __future__ import annotations

import datetime
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from ouroboros.agent.task_pipeline import emit_task_results
from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_store import init_runtime_store
from supervisor.cancellation import EventCancellation
from supervisor.queue import TaskQueue
from supervisor.state import get_state_repository
from supervisor.workers import _STOP_GRACE_SECONDS, TaskExecutor, WorkerSlot


def _iso_ago(seconds: float) -> str:
    return (datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=seconds)).isoformat()


def _queue(tmp_path, store):
    return TaskQueue(store, tmp_path, soft_timeout=1, hard_timeout=60, state_repo=get_state_repository(tmp_path))


def _running_task(store, task_id: str, *, age_sec: float = 120.0, task_type: str = "task"):
    record = store.create_task(
        TaskRequest(id=task_id, type=task_type, text="hangs forever", chat_id=42, source="dialogs"),
        status="running",
    )
    record.started_at = _iso_ago(age_sec)
    record.heartbeat_at = record.started_at
    record.worker_id = 0
    return store.upsert_task(record)


class _SlotExecutor:
    """Just enough executor for the queue's kill and cancel paths."""

    def __init__(self, slot: WorkerSlot) -> None:
        self.slot = slot
        self.restarts = 0

    def get_slot(self, worker_id):
        return self.slot if worker_id == self.slot.worker_id else None

    def get_slot_for_task(self, task_id):
        return self.slot if self.slot.busy_task_id == task_id else None

    def ensure_started(self):
        self.restarts += 1


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


def test_hard_timeout_sets_the_stop_signal_and_keeps_the_slot(tmp_path) -> None:
    store = init_runtime_store(tmp_path)
    _running_task(store, "hung")
    slot = _claimed_slot()
    executor = _SlotExecutor(slot)

    _queue(tmp_path, store).enforce_task_timeouts(executor)

    assert slot.cancellation.is_stopped()
    assert slot.stop_requested_at is not None
    # The slot is not handed back while the body it belongs to is still running.
    assert slot.busy_task_id == "hung"
    assert slot.future is not None
    assert store.get_task("hung").status == "failed"


def _recording_slot(store, task_id: str = "hung"):
    """A claimed slot that notes the stored status of its task each time it is asked to stop."""
    status_at_stop: list[str] = []

    class _RecordingSlot(WorkerSlot):
        def request_stop(self) -> None:
            status_at_stop.append(store.get_task(task_id).status)
            super().request_stop()

    return _RecordingSlot(worker_id=0, busy_task_id=task_id, future=Future()), status_at_stop


def test_the_stop_is_set_only_after_the_record_is_terminal(tmp_path) -> None:
    """A body returning in that window has to find its task already closed, or it publishes."""
    store = init_runtime_store(tmp_path)
    _running_task(store, "hung")
    slot, status_at_stop = _recording_slot(store)

    _queue(tmp_path, store).enforce_task_timeouts(_SlotExecutor(slot))

    assert status_at_stop == ["failed"]


def test_shutdown_sets_the_stop_only_after_the_records_are_terminal(tmp_path) -> None:
    store = init_runtime_store(tmp_path)
    _running_task(store, "hung")
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    slot, status_at_stop = _recording_slot(store)
    executor._slots[0] = slot

    executor.shutdown()

    assert status_at_stop == ["failed"]


def test_cancelling_a_running_task_sets_the_stop_signal_and_keeps_the_slot(tmp_path) -> None:
    store = init_runtime_store(tmp_path)
    _running_task(store, "hung", age_sec=1.0)
    slot = _claimed_slot()
    executor = _SlotExecutor(slot)

    assert _queue(tmp_path, store).cancel_task_by_id("hung", executor=executor) is True

    assert slot.cancellation.is_stopped()
    assert slot.busy_task_id == "hung"
    assert store.get_task("hung").status == "cancelled"


def test_a_second_stop_request_does_not_push_out_the_grace_period() -> None:
    slot = _claimed_slot()
    slot.request_stop()
    first = slot.stop_requested_at
    time.sleep(0.01)
    slot.request_stop()
    assert slot.stop_requested_at == first


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
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_queue=_queue(tmp_path, store),
        store=store,
    )


def test_a_stopped_body_returns_and_releases_the_agent_lock(tmp_path) -> None:
    store = init_runtime_store(tmp_path)
    record = _running_task(store, "hung")
    executor = _executor(tmp_path, store)
    agent = _LoopingAgent()
    executor.get_agent = lambda: agent  # type: ignore[method-assign]
    slot = _claimed_slot()

    thread = threading.Thread(target=executor._run_task, args=(record, slot.cancellation), daemon=True)
    thread.start()
    assert agent.entered.wait(timeout=5)
    assert executor._agent_lock.locked()

    slot.request_stop()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert agent.saw_signal
    assert agent.rounds >= 1
    # The lock the zombie used to hold for its whole life is back for the next task.
    assert not executor._agent_lock.locked()


def test_the_body_observes_the_signal_it_was_handed(tmp_path) -> None:
    store = init_runtime_store(tmp_path)
    record = _running_task(store, "plain")
    executor = _executor(tmp_path, store)
    slot = _claimed_slot(task_id="plain")
    seen: list = []

    class _Agent:
        def handle_task(self, task, *, cancellation):
            seen.append(cancellation)
            return []

    executor.get_agent = lambda: _Agent()  # type: ignore[method-assign]
    executor._run_task(record, slot.cancellation)

    assert seen == [slot.cancellation]


def test_a_body_is_handed_the_signal_of_the_claim_that_started_it(tmp_path) -> None:
    """Read at submit time, not in the worker thread: by then the slot may have moved on."""
    store = init_runtime_store(tmp_path)
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    executor.task_queue.enqueue_dict({"id": "first", "type": "task", "text": "go", "chat_id": 42})
    handed: list = []
    executor._run_task = lambda _record, cancellation: handed.append(cancellation)  # type: ignore[method-assign]
    slot = executor._slots[0]

    assert executor._assign_slot(slot) is not None
    claimed_signal = slot.cancellation
    slot.future.result(timeout=5)
    slot.clear_task()

    assert handed == [claimed_signal]
    assert handed[0] is not slot.cancellation


# --- the slot stays claimed, and abandonment is visible ---------------------


def test_no_task_is_claimed_while_a_stopping_body_still_holds_the_slot(tmp_path) -> None:
    store = init_runtime_store(tmp_path)
    _running_task(store, "hung")
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    queued = executor.task_queue.enqueue_dict({"id": "next", "type": "task", "text": "waiting", "chat_id": 42})
    slot = _claimed_slot()
    executor._slots[0] = slot
    slot.request_stop()

    assert executor._assign_slot(slot) is None
    assert store.get_task(queued.task_id).status == "queued"
    assert slot.busy_task_id == "hung"


def test_a_body_that_stops_in_time_frees_its_slot_without_an_abandonment(tmp_path, monkeypatch) -> None:
    store = init_runtime_store(tmp_path)
    executor = _executor(tmp_path, store)
    events: list = []
    monkeypatch.setattr(
        "supervisor.workers.record_event", lambda _root, _cat, name, payload: events.append((name, payload))
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
    store = init_runtime_store(tmp_path)
    executor = _executor(tmp_path, store)
    events: list = []
    monkeypatch.setattr(
        "supervisor.workers.record_event", lambda _root, _cat, name, payload: events.append((name, payload))
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


def test_an_abandoned_foreground_slot_is_not_reused_over_the_held_agent_lock(tmp_path, monkeypatch) -> None:
    """The leaked thread keeps the lock and the agent it is inside; the next task gets new ones."""
    store = init_runtime_store(tmp_path)
    executor = _executor(tmp_path, store)
    monkeypatch.setattr("supervisor.workers.record_event", lambda *_args, **_kwargs: None)
    slot = _claimed_slot()
    executor._slots[0] = slot
    slot.request_stop()
    slot.stop_requested_at = time.monotonic() - _STOP_GRACE_SECONDS - 1
    leaked_lock, leaked_factory = executor._agent_lock, executor._agent_factory
    leaked_lock.acquire()

    executor.ensure_healthy()

    assert slot.busy_task_id is None
    assert executor._agent_lock is not leaked_lock
    assert executor._agent_factory is not leaked_factory
    assert executor._agent_lock.acquire(timeout=1)


def test_an_abandonment_hands_the_remaining_slots_a_fresh_pool(tmp_path, monkeypatch) -> None:
    """The leaked thread keeps a worker of the old pool, so the next claim must not queue behind it."""
    store = init_runtime_store(tmp_path)
    executor = _executor(tmp_path, store)
    monkeypatch.setattr("supervisor.workers.record_event", lambda *_args, **_kwargs: None)
    executor.ensure_started()
    leaked_pool = executor._pool
    slot = _claimed_slot()
    executor._slots[0] = slot
    slot.request_stop()
    slot.stop_requested_at = time.monotonic() - _STOP_GRACE_SECONDS - 1

    executor.ensure_healthy()

    assert executor._pool is not leaked_pool
    # A task claimed after the leak runs, instead of waiting for a worker that never returns.
    executor.task_queue.enqueue_dict({"id": "next", "type": "task", "text": "go", "chat_id": 42})
    ran: list = []
    executor._run_task = lambda _record, _cancellation: ran.append(_record.task_id)  # type: ignore[method-assign]
    assert executor._assign_slot(executor._slots[0]) is not None
    executor._slots[0].future.result(timeout=5)
    assert ran == ["next"]


def test_a_slot_that_was_never_stopped_is_left_alone(tmp_path, monkeypatch) -> None:
    store = init_runtime_store(tmp_path)
    executor = _executor(tmp_path, store)
    events: list = []
    monkeypatch.setattr(
        "supervisor.workers.record_event", lambda _root, _cat, name, payload: events.append((name, payload))
    )
    slot = _claimed_slot()
    executor._slots[0] = slot

    executor.ensure_healthy()

    assert slot.busy_task_id == "hung"
    assert events == []


# --- the background lanes ---------------------------------------------------


def test_executor_shutdown_stops_every_running_task_through_its_own_signal(tmp_path) -> None:
    store = init_runtime_store(tmp_path)
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


def test_consolidation_receives_the_per_task_signal(tmp_path, monkeypatch) -> None:
    store = init_runtime_store(tmp_path)
    record = _running_task(store, "cons", task_type="context_consolidation")
    executor = _executor(tmp_path, store)
    slot = _claimed_slot(task_id="cons", worker_id=1)
    captured: list = []

    def fake_run(env, llm, task_input, *, cancellation, foreground_pending, event_queue, task_id):
        captured.append(cancellation)
        # A continuation short-circuits the commit path: nothing to schedule, nothing to emit.
        return {"resume_from": "x"}, None, "partial"

    monkeypatch.setattr("ouroboros.agent.context_consolidation.run_context_consolidation_task", fake_run)
    executor._run_task(record, slot.cancellation)

    assert captured == [slot.cancellation]
    assert captured[0].is_stopped() is False
    slot.request_stop()
    assert captured[0].is_stopped() is True


def test_distillation_observes_no_stop_signal_but_its_slot_is_still_bounded(tmp_path, monkeypatch) -> None:
    """The lane observes nothing; the grace period is what bounds it."""
    import inspect

    from ouroboros.agent.task_pipeline import run_memory_distillation_task

    assert "cancellation" not in inspect.signature(run_memory_distillation_task).parameters

    store = init_runtime_store(tmp_path)
    executor = _executor(tmp_path, store)
    events: list = []
    monkeypatch.setattr(
        "supervisor.workers.record_event", lambda _root, _cat, name, payload: events.append((name, payload))
    )
    slot = _claimed_slot(task_id="distill", worker_id=1)
    executor._slots[1] = slot
    slot.request_stop()
    slot.stop_requested_at = time.monotonic() - _STOP_GRACE_SECONDS - 1
    factory = executor._agent_factory

    executor.ensure_healthy()

    assert slot.busy_task_id is None
    assert [payload["task_id"] for name, payload in events if name == "task_abandoned"] == ["distill"]
    # This lane never takes the agent lock, so releasing it throws no agent away.
    assert executor._agent_factory is factory


# --- the late-result policy -------------------------------------------------


@pytest.fixture()
def killed_task(tmp_path):
    """A record already failed by the hard timeout, as a late body would find it."""
    store = init_runtime_store(tmp_path)
    store.create_task(
        TaskRequest(id="killed", type="task", text="do it", chat_id=7, source="dialogs"), status="running"
    )
    store.fail_task("killed", result="Task killed by hard timeout after 1800s.")
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
        has_answer=has_answer,
        stopped_externally=stopped_externally,
    )
    return pending


def _of_type(pending, event_type):
    return [event for event in pending if event["type"] == event_type]


def test_a_late_answer_reaches_the_owner_labelled_as_late(tmp_path, killed_task) -> None:
    killed_at = killed_task.get_task("killed").finished_at
    pending = _emit(tmp_path, "killed", "here is the answer")

    sent = _of_type(pending, "send_message")
    assert len(sent) == 1
    assert sent[0]["text"].startswith("⏳ Late result")
    assert "hard timeout" in sent[0]["text"]
    assert sent[0]["text"].endswith("here is the answer")
    # The record keeps the status, the result and the completion time that closed the task.
    record = killed_task.get_task("killed")
    assert record.status == "failed"
    assert record.result == "Task killed by hard timeout after 1800s."
    assert record.finished_at == killed_at
    assert sent[0]["text"].startswith(f"⏳ Late result — this task had already been stopped at {killed_at}")


def test_a_cooperatively_stopped_task_sends_no_owner_message(tmp_path, killed_task) -> None:
    pending = _emit(
        tmp_path,
        "killed",
        "⚠️ Model returned an empty response. Try rephrasing your request.",
        has_answer=False,
    )

    assert _of_type(pending, "send_message") == []
    # Everything that is not owner-facing still runs.
    assert len(_of_type(pending, "task_done")) == 1
    assert len(_of_type(pending, "task_metrics")) == 1


def test_a_run_that_failed_itself_publishes_its_error_plainly(tmp_path, killed_task) -> None:
    """The crash path fails the record before publishing, so the record alone cannot tell why."""
    error = "⚠️ Error during processing: RuntimeError: boom"

    sent = _of_type(_emit(tmp_path, "killed", error, stopped_externally=False), "send_message")

    assert len(sent) == 1
    assert sent[0]["text"] == error


def test_an_active_record_publishes_exactly_as_before(tmp_path) -> None:
    store = init_runtime_store(tmp_path)
    store.create_task(TaskRequest(id="live", type="task", text="do it", chat_id=7, source="dialogs"), status="running")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    sent = _of_type(_emit(tmp_path, "live", "here is the answer"), "send_message")

    assert len(sent) == 1
    assert sent[0]["text"] == "here is the answer"


def test_emit_result_reports_a_missing_answer_to_the_pipeline(tmp_path) -> None:
    """The empty-text fallback erases the difference, so the adapter reports it separately."""
    from unittest.mock import patch

    from ouroboros.agent.loop_adapters import EventSinkAdapter

    adapter = EventSinkAdapter(
        emit_progress=lambda _t: None,
        event_queue=None,
        task_id="t",
        drive_logs=tmp_path,
        env=SimpleNamespace(drive_root=tmp_path),
        llm=object(),
        pending_events=[],
        task={"id": "t", "chat_id": 0},
        start_time=1.0,
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
