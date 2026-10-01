"""Regression: a hung task body produces at most one hard-timeout retry.

The 2026-08-11 incident: `enforce_task_timeouts` failed an overdue task and enqueued a
retry, the body it could not stop kept heartbeating, `heartbeat_task` wrote the record back
to `running`, and the next tick cloned it again — 50 clones in 25 minutes.
"""

from __future__ import annotations

import datetime

from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_store import init_runtime_store
from supervisor.queue import QUEUE_MAX_RETRIES, TaskQueue
from supervisor.state import get_state_repository


class _NoSlotExecutor:
    """The hard-timeout path finds no slot to cancel, exactly as it does for a hung body."""

    def get_slot(self, worker_id):
        return None

    def ensure_started(self):
        pass


def _iso_ago(seconds: float) -> str:
    moment = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
    return moment.isoformat()


def _start_overdue_task(store, task_id: str, *, age_sec: float = 120.0):
    record = store.create_task(
        TaskRequest(id=task_id, type="task", text="hangs forever", chat_id=42, source="dialogs"),
        status="running",
    )
    record.started_at = _iso_ago(age_sec)
    record.heartbeat_at = record.started_at
    return store.upsert_task(record)


def _queue(tmp_path, store):
    return TaskQueue(store, tmp_path, soft_timeout=1, hard_timeout=60, state_repo=get_state_repository(tmp_path))


def _capture_outbound(monkeypatch) -> list:
    """Owner notices leave through `send_outbound_message`, the one outbound door."""
    sent: list = []
    monkeypatch.setattr("supervisor.queue.send_outbound_message", lambda *args, **kwargs: sent.append((args, kwargs)))
    return sent


def _clones(store, original_id: str) -> list:
    return [record for record in store.list_tasks(limit=500) if record.input.get("timeout_retry_from") == original_id]


def test_hung_task_produces_one_clone_across_many_ticks(tmp_path) -> None:
    store = init_runtime_store(tmp_path)
    _start_overdue_task(store, "hung")
    queue = _queue(tmp_path, store)
    executor = _NoSlotExecutor()

    for _ in range(5):
        queue.enforce_task_timeouts(executor)
        # The body is still alive and keeps announcing itself.
        store.heartbeat_task("hung", phase="running")

    assert store.get_task("hung").status == "failed"
    clones = _clones(store, "hung")
    assert len(clones) == 1
    assert clones[0].attempt == QUEUE_MAX_RETRIES + 1


def test_heartbeat_cannot_resurrect_a_hard_timed_out_task(tmp_path) -> None:
    store = init_runtime_store(tmp_path)
    _start_overdue_task(store, "hung")
    queue = _queue(tmp_path, store)

    queue.enforce_task_timeouts(_NoSlotExecutor())
    failed_at = store.get_task("hung").heartbeat_at
    store.heartbeat_task("hung", phase="running")

    record = store.get_task("hung")
    assert record.status == "failed"
    assert record.heartbeat_at == failed_at
    assert "hung" not in queue.list_running()


def test_resurrected_record_is_not_retried_again(tmp_path) -> None:
    store = init_runtime_store(tmp_path)
    _start_overdue_task(store, "hung")
    queue = _queue(tmp_path, store)
    executor = _NoSlotExecutor()

    queue.enforce_task_timeouts(executor)
    # Force the record back to running the way only an unguarded write door could.
    revived = store.get_task("hung")
    revived.status = "running"
    revived.started_at = _iso_ago(120.0)
    store.upsert_task(revived)
    queue.enforce_task_timeouts(executor)

    assert len(_clones(store, "hung")) == 1


def test_retry_clone_that_times_out_is_not_retried_further(tmp_path) -> None:
    store = init_runtime_store(tmp_path)
    _start_overdue_task(store, "hung")
    queue = _queue(tmp_path, store)
    executor = _NoSlotExecutor()

    queue.enforce_task_timeouts(executor)
    clone = _clones(store, "hung")[0]
    clone.status = "running"
    clone.started_at = _iso_ago(120.0)
    clone.heartbeat_at = clone.started_at
    store.upsert_task(clone)
    queue.enforce_task_timeouts(executor)

    assert store.get_task(clone.task_id).status == "failed"
    assert _clones(store, clone.task_id) == []


def test_timeout_notices_go_through_the_outbound_fanout(tmp_path, monkeypatch) -> None:
    store = init_runtime_store(tmp_path)
    _start_overdue_task(store, "hung")
    sent = _capture_outbound(monkeypatch)

    _queue(tmp_path, store).enforce_task_timeouts(_NoSlotExecutor())

    texts = [call[0][1] for call in sent]
    assert any(text.startswith("⏱️") for text in texts)
    assert any("🛑 Hard-timeout" in text for text in texts)
    assert all(call[0][0] == 42 for call in sent)


def test_exhausted_retry_notice_goes_through_the_outbound_fanout(tmp_path, monkeypatch) -> None:
    store = init_runtime_store(tmp_path)
    record = _start_overdue_task(store, "hung")
    record.attempt = QUEUE_MAX_RETRIES + 1
    store.upsert_task(record)
    sent = _capture_outbound(monkeypatch)

    _queue(tmp_path, store).enforce_task_timeouts(_NoSlotExecutor())

    assert _clones(store, "hung") == []
    assert any("Retry limit exhausted" in call[0][1] for call in sent)


def test_a_task_completed_during_the_sweep_is_neither_retried_nor_announced(tmp_path, monkeypatch) -> None:
    """The body closes the record while the sweep is inside a notice, so the kill is refused."""
    store = init_runtime_store(tmp_path)
    _start_overdue_task(store, "done")
    sent: list = []

    def finish_the_body_then_send(*args, **_kwargs):
        sent.append(args)
        store.complete_task("done", result="the answer")

    monkeypatch.setattr("supervisor.queue.send_outbound_message", finish_the_body_then_send)

    _queue(tmp_path, store).enforce_task_timeouts(_NoSlotExecutor())

    record = store.get_task("done")
    assert record.status == "completed"
    assert record.result == "the answer"
    assert _clones(store, "done") == []
    # Only the soft notice: the owner is never told a completed task was killed.
    assert len(sent) == 1
    assert sent[0][1].startswith("⏱️")


def test_a_transport_that_is_not_ready_does_not_stop_the_sweep(tmp_path, monkeypatch) -> None:
    store = init_runtime_store(tmp_path)
    _start_overdue_task(store, "hung")
    monkeypatch.setattr(
        "supervisor.queue.send_outbound_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("transport is not ready")),
    )

    _queue(tmp_path, store).enforce_task_timeouts(_NoSlotExecutor())

    assert store.get_task("hung").status == "failed"
    assert len(_clones(store, "hung")) == 1
