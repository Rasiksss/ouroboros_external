"""Regression: a hung task body produces at most one hard-timeout retry.

The 2026-08-11 incident: `enforce_task_timeouts` failed an overdue task and enqueued a
retry, the body it could not stop kept heartbeating, `heartbeat_task` wrote the record back
to `running`, and the next tick cloned it again — 50 clones in 25 minutes.

The ceiling bounds the deployment, not one process: the sweep runs on every supervisor
instance, so two of them listing the same overdue record must still produce one clone and
one notice.
"""

from __future__ import annotations

import datetime

import pytest

from new_core.coordination.inprocess import NullWorkSignal
from ouroboros.observability.event_record import list_runtime_events
from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.queue import QUEUE_MAX_RETRIES, TaskQueue
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.shared import TEST_SCOPE, scope_with
from transport.interfaces import routed_chat_id


def _iso_ago(seconds: float) -> str:
    moment = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
    return moment.isoformat()


#: The conversation an overdue task belongs to. One value: the row's `chat_id` column and the
#: scope stored beside it have to name the same chat, or the notice is filed under one
#: conversation and delivered to another.
_TASK_SCOPE = scope_with(TEST_SCOPE, surface_id="dialogs", chat_id="42")


def _start_overdue_task(store, task_id: str, *, age_sec: float = 120.0, detached: bool = False, scope=_TASK_SCOPE):
    record = store.tasks_repo.create_task(
        TaskRequest(
            id=task_id,
            detached=detached,
            type="task",
            text="hangs forever",
            chat_id=int(scope.chat_id),
            source=scope.surface_id,
            scope=scope.to_dict(),
        ),
        status="running",
    )
    record.started_at = _iso_ago(age_sec)
    record.metadata["run_started_at"] = record.started_at
    record.heartbeat_at = _iso_ago(0) if detached else record.started_at
    return store.tasks_repo.upsert_task(record)


def _queue(tmp_path, store):
    return TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=1,
        hard_timeout=60,
        audit_event_repository=sql_audit_event_repository(store.engine),
    )


def _capture_outbound(monkeypatch) -> list:
    """Owner notices leave through `send_with_budget`, the one outbound door."""
    sent: list = []
    monkeypatch.setattr("supervisor.queue.send_with_budget", lambda *args, **kwargs: sent.append((args, kwargs)))
    return sent


def _clones(store, original_id: str) -> list:
    return [
        record
        for record in store.tasks_repo.list_tasks(limit=500)
        if record.input.get("timeout_retry_from") == original_id
    ]


def _shared_snapshot(monkeypatch, queue) -> None:
    """What two instances see when both list the record before either of them writes.

    The listing is taken once and handed to every sweep, which is the whole of the race: a
    flag or a status read there is the same for both, and only the write can tell them apart.
    """
    snapshot = queue.list_running()
    monkeypatch.setattr(TaskQueue, "list_running", lambda _self, **_kwargs: snapshot)


def _overdue_task_nobody_can_be_told_about(store, task_id: str, *, age_sec: float):
    """An overdue record whose stored input names no scope and whose columns recover none.

    `_notice_route` answers `None` for it, so there is no owner to address: `chat_id` is
    empty, so the row's own columns cannot reconstruct a scope either.
    """
    record = store.tasks_repo.create_task(
        TaskRequest(id=task_id, type="task", text="hangs forever", source="dialogs", scope=_TASK_SCOPE.to_dict()),
        status="running",
    )
    record.started_at = _iso_ago(age_sec)
    record.heartbeat_at = record.started_at
    record.chat_id = None
    record.input = {key: value for key, value in record.input.items() if key != "scope"}
    return store.tasks_repo.upsert_task(record)


def test_a_record_nobody_can_be_told_about_does_not_spend_its_claim(tmp_path, monkeypatch) -> None:
    """The claim records that a notice went out, so an unaddressable record must not take it.

    Claiming first and addressing afterwards would leave `soft_notice_at` set on a record
    whose owner was never told anything — and no later sweep would announce it either.
    """
    store = runtime_persistence.initialize()
    _overdue_task_nobody_can_be_told_about(store, "orphan", age_sec=5.0)
    sent = _capture_outbound(monkeypatch)

    _queue(tmp_path, store).enforce_task_timeouts()

    assert sent == []
    assert store.tasks_repo.get_task("orphan").soft_notice_at is None


def test_two_sweepers_on_one_listing_retry_and_announce_once(tmp_path, monkeypatch) -> None:
    store = runtime_persistence.initialize()
    _start_overdue_task(store, "hung")
    queue_a, queue_b = _queue(tmp_path, store), _queue(tmp_path, store)
    _shared_snapshot(monkeypatch, queue_a)
    sent = _capture_outbound(monkeypatch)

    queue_a.enforce_task_timeouts()
    queue_b.enforce_task_timeouts()

    assert len(_clones(store, "hung")) == 1
    assert len([call for call in sent if "🛑 Hard-timeout" in call[0][0]]) == 1
    events = list_runtime_events(
        log_stream="supervisor",
        event_type="task_hard_timeout",
        audit_event_repository=sql_audit_event_repository(),
    )
    assert len(events) == 1


def test_timeout_sweep_finds_an_overdue_run_beyond_the_default_listing_batch(tmp_path, monkeypatch) -> None:
    store = runtime_persistence.initialize()
    for index in range(501):
        _start_overdue_task(
            store, f"healthy-{index:03d}", age_sec=0.0, scope=scope_with(_TASK_SCOPE, owner_id=f"healthy-{index}")
        )
    _start_overdue_task(store, "hung")
    _capture_outbound(monkeypatch)

    _queue(tmp_path, store).enforce_task_timeouts()

    assert store.tasks_repo.get_task("hung").status == "failed"


def test_two_sweepers_past_the_soft_timeout_announce_once(tmp_path, monkeypatch) -> None:
    store = runtime_persistence.initialize()
    _start_overdue_task(store, "slow", age_sec=5.0)
    queue_a, queue_b = _queue(tmp_path, store), _queue(tmp_path, store)
    _shared_snapshot(monkeypatch, queue_a)
    sent = _capture_outbound(monkeypatch)

    queue_a.enforce_task_timeouts()
    queue_b.enforce_task_timeouts()

    assert store.tasks_repo.get_task("slow").status == "running"
    assert len([call for call in sent if call[0][0].startswith("⏱️")]) == 1


def test_a_record_that_left_running_before_the_claim_is_not_announced(tmp_path, monkeypatch) -> None:
    """The notice says the task is still running, so a closed record is not announced."""
    store = runtime_persistence.initialize()
    _start_overdue_task(store, "slow", age_sec=5.0)
    queue = _queue(tmp_path, store)
    _shared_snapshot(monkeypatch, queue)
    sent = _capture_outbound(monkeypatch)
    store.tasks_repo.complete_task("slow", result="the answer")

    queue.enforce_task_timeouts()

    assert sent == []


def test_hung_task_produces_one_clone_across_many_ticks(tmp_path) -> None:
    store = runtime_persistence.initialize()
    _start_overdue_task(store, "hung")
    queue = _queue(tmp_path, store)

    for _ in range(5):
        queue.enforce_task_timeouts()
        # The body is still alive and keeps announcing itself.
        store.tasks_repo.heartbeat_task("hung", phase="running")

    assert store.tasks_repo.get_task("hung").status == "failed"
    clones = _clones(store, "hung")
    assert len(clones) == 1
    assert clones[0].attempt == QUEUE_MAX_RETRIES + 1


def test_hard_timeout_does_not_retry_a_stopped_detached_job(tmp_path) -> None:
    store = runtime_persistence.initialize()
    _start_overdue_task(store, "stopped-job", detached=True)
    store.tasks_repo.stop_jobs(scope=_TASK_SCOPE, job_id="stopped-job")

    _queue(tmp_path, store).enforce_task_timeouts()

    assert store.tasks_repo.get_task("stopped-job").status == "cancelled"
    assert _clones(store, "stopped-job") == []


@pytest.mark.parametrize("transition", ["hard_timeout", "lease_lost", "instance_released"])
def test_stop_after_retry_candidate_creation_preserves_the_committed_stop(tmp_path, monkeypatch, transition) -> None:
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="stopped-race", text="work", scope=_TASK_SCOPE.to_dict()))
    record = store.tasks_repo.claim_next_task(runtime_id="runtime")
    record.started_at = record.heartbeat_at = _iso_ago(180)
    store.tasks_repo.upsert_task(record)
    queue = _queue(tmp_path, store)
    queue.soft_timeout_sec = 1_000
    signals: list[bool] = []
    notices = _capture_outbound(monkeypatch)
    monkeypatch.setattr(queue, "announce_claimable", lambda: signals.append(True))
    original = store.tasks_repo.transition_running

    def stop_before_transition(*args, **kwargs):
        store.tasks_repo.stop_jobs(scope=_TASK_SCOPE, job_id=record.task_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(store.tasks_repo, "transition_running", stop_before_transition)
    if transition == "hard_timeout":
        queue.enforce_task_timeouts()
    elif transition == "lease_lost":
        queue.reclaim_stale_leases()
    else:
        queue.release_own_runs(runtime_id="runtime")

    assert store.tasks_repo.get_task(record.task_id).status == "cancelled"
    assert len(store.tasks_repo.list_tasks()) == 1
    assert signals == []
    assert [call[0][0] for call in notices] == [f"🛑 Task {record.task_id} stopped as requested."]
    events = list_runtime_events(log_stream="supervisor", audit_event_repository=sql_audit_event_repository())
    assert any(event.get("requeued") is False for event in events)


def test_heartbeat_cannot_resurrect_a_hard_timed_out_task(tmp_path) -> None:
    store = runtime_persistence.initialize()
    _start_overdue_task(store, "hung")
    queue = _queue(tmp_path, store)

    queue.enforce_task_timeouts()
    failed_at = store.tasks_repo.get_task("hung").heartbeat_at
    store.tasks_repo.heartbeat_task("hung", phase="running")

    record = store.tasks_repo.get_task("hung")
    assert record.status == "failed"
    assert record.heartbeat_at == failed_at
    assert "hung" not in queue.list_running()


def test_resurrected_record_is_not_retried_again(tmp_path) -> None:
    store = runtime_persistence.initialize()
    _start_overdue_task(store, "hung")
    queue = _queue(tmp_path, store)

    queue.enforce_task_timeouts()
    # Force the record back to running the way only an unguarded write door could.
    revived = store.tasks_repo.get_task("hung")
    revived.status = "running"
    revived.started_at = _iso_ago(120.0)
    store.tasks_repo.upsert_task(revived)
    queue.enforce_task_timeouts()

    assert len(_clones(store, "hung")) == 1


def test_retry_clone_that_times_out_is_not_retried_further(tmp_path) -> None:
    store = runtime_persistence.initialize()
    _start_overdue_task(store, "hung")
    queue = _queue(tmp_path, store)

    queue.enforce_task_timeouts()
    clone = _clones(store, "hung")[0]
    clone.status = "running"
    clone.started_at = _iso_ago(120.0)
    clone.heartbeat_at = clone.started_at
    store.tasks_repo.upsert_task(clone)
    queue.enforce_task_timeouts()

    assert store.tasks_repo.get_task(clone.task_id).status == "failed"
    assert _clones(store, clone.task_id) == []


def test_timeout_notices_go_through_the_outbound_fanout(tmp_path, monkeypatch) -> None:
    store = runtime_persistence.initialize()
    _start_overdue_task(store, "hung")
    sent = _capture_outbound(monkeypatch)

    _queue(tmp_path, store).enforce_task_timeouts()

    texts = [call[0][0] for call in sent]
    assert any(text.startswith("⏱️") for text in texts)
    assert any("🛑 Hard-timeout" in text for text in texts)
    assert all(routed_chat_id(call[1]["scope"]) == 42 for call in sent)


def test_exhausted_retry_notice_goes_through_the_outbound_fanout(tmp_path, monkeypatch) -> None:
    store = runtime_persistence.initialize()
    record = _start_overdue_task(store, "hung")
    record.attempt = QUEUE_MAX_RETRIES + 1
    store.tasks_repo.upsert_task(record)
    sent = _capture_outbound(monkeypatch)

    _queue(tmp_path, store).enforce_task_timeouts()

    assert _clones(store, "hung") == []
    assert any("Retry limit exhausted" in call[0][0] for call in sent)


def test_a_task_completed_during_the_sweep_is_neither_retried_nor_announced(tmp_path, monkeypatch) -> None:
    """The body closes the record while the sweep is inside a notice, so the kill is refused."""
    store = runtime_persistence.initialize()
    _start_overdue_task(store, "done")
    sent: list = []

    def finish_the_body_then_send(*args, **_kwargs):
        sent.append(args)
        store.tasks_repo.complete_task("done", result="the answer")

    monkeypatch.setattr("supervisor.queue.send_with_budget", finish_the_body_then_send)

    _queue(tmp_path, store).enforce_task_timeouts()

    record = store.tasks_repo.get_task("done")
    assert record.status == "completed"
    assert record.result == "the answer"
    assert _clones(store, "done") == []
    # Only the soft notice: the owner is never told a completed task was killed.
    assert len(sent) == 1
    assert sent[0][0].startswith("⏱️")


def test_a_transport_that_is_not_ready_does_not_stop_the_sweep(tmp_path, monkeypatch) -> None:
    store = runtime_persistence.initialize()
    _start_overdue_task(store, "hung")
    monkeypatch.setattr(
        "supervisor.queue.send_with_budget",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("transport is not ready")),
    )

    _queue(tmp_path, store).enforce_task_timeouts()

    assert store.tasks_repo.get_task("hung").status == "failed"
    assert len(_clones(store, "hung")) == 1
