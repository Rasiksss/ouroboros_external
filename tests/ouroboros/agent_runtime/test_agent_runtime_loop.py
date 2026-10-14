"""The runtime claims its own work, on its own schedule, and hands it back on the way out.

Every case here drives the real loop over real records. What is faked is the body: a run's
agent is replaced by one that blocks or returns, because what is under test is when a task
is claimed and started, never what the model says.
"""

from __future__ import annotations

import contextlib
import threading
import time

import pytest
from sqlalchemy.exc import ProgrammingError

from new_core.coordination.inprocess import NullWorkSignal
from new_core.coordination.interfaces import TaskStopNotice
from new_core.identity import OwnerScope
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.queue import RELEASE_ERROR_CODE, TaskQueue
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.database import refused_connections
from tests.helpers.shared import TEST_SCOPE, scope_with
from tests.helpers.supervisor import bootstrap_agent_runtime_for_tests

#: Long enough for a slow machine, short enough that a hang fails rather than hangs.
_DEADLINE_SEC = 10.0
#: The drain spends a fifth of this on the release itself, so 0.4s rather than 40ms on a loaded runner.
_IN_FLIGHT_DRAIN_SEC = 2.0


class _BlockingAgent:
    """A body that announces it started, waits to be let go, then closes its own record.

    Closing the record is not scaffolding: a real body writes its terminal status itself,
    and that write is what makes the owner's next task claimable again.
    """

    def __init__(self, drive_root) -> None:
        self.drive_root = drive_root
        self.entered = threading.Event()
        self.release = threading.Event()

    def handle_task(self, task, *, cancellation):
        from ouroboros.storage.task_results import STATUS_COMPLETED, write_task_result

        self.entered.set()
        self.release.wait(_DEADLINE_SEC)
        write_task_result(
            self.drive_root,
            str(task.get("id") or ""),
            STATUS_COMPLETED,
            scope=OwnerScope.from_dict(task["scope"]),
            result="done",
        )
        return []


class _StopCompletingAgent:
    """A body that turns a local stop into the graph's empty-result completion path."""

    def __init__(self, drive_root, store) -> None:
        self.drive_root = drive_root
        self.store = store
        self.entered = threading.Event()
        self.returned = threading.Event()

    def handle_task(self, task, *, cancellation):
        from ouroboros.storage.task_results import STATUS_COMPLETED, write_task_result

        self.entered.set()
        while not cancellation.is_stopped():
            time.sleep(0.001)
        write_task_result(
            self.drive_root,
            str(task.get("id") or ""),
            STATUS_COMPLETED,
            scope=OwnerScope.from_dict(task["scope"]),
            result="",
        )
        self.returned.set()
        return []


class _PerTaskAgents:
    """One blocking body per task id, so a test can let one go without letting the others."""

    def __init__(self, drive_root) -> None:
        self.drive_root = drive_root
        self.by_task: dict[str, _BlockingAgent] = {}
        self._lock = threading.Lock()
        self._shared = _RoutingAgent(self)

    def new_agent(self):
        return self._shared

    def of(self, task_id: str) -> _BlockingAgent:
        with self._lock:
            return self.by_task.setdefault(task_id, _BlockingAgent(self.drive_root))

    def wait_for(self, task_id: str, timeout: float = _DEADLINE_SEC) -> bool:
        return self.of(task_id).entered.wait(timeout)


class _RoutingAgent:
    """The agent every run is handed; it routes each task to that task's own body."""

    def __init__(self, agents: _PerTaskAgents) -> None:
        self._agents = agents

    def handle_task(self, task, *, cancellation):
        return self._agents.of(str(task.get("id") or "")).handle_task(task, cancellation=cancellation)


def _runtime(
    tmp_path,
    monkeypatch,
    *,
    slots: int = 1,
    poll_interval_sec: float = 0.05,
    work_signal=None,
    task_stops=None,
):
    monkeypatch.setattr("ouroboros.agent_runtime.executor._configured_worker_slots", lambda: slots)
    parts = bootstrap_agent_runtime_for_tests(
        tmp_path,
        poll_interval_sec=poll_interval_sec,
        work_signal=work_signal,
        task_stops=task_stops,
    )
    agents = _PerTaskAgents(tmp_path)
    parts.runtime._executor.new_agent = agents.new_agent  # type: ignore[method-assign]
    return parts, agents


def _enqueue(tmp_path, store, task_id: str, *, owner: str = "global", detached: bool = False) -> None:
    """Put a task on the queue the way the supervisor does, through its own handle."""
    scope = scope_with(TEST_SCOPE, owner_id=owner)
    TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    ).enqueue_dict(
        {
            "id": task_id,
            "type": "task",
            "text": "go",
            "chat_id": str(scope.chat_id),
            "scope": scope.to_dict(),
            "detached": detached,
        }
    )


def _run_in_background(parts) -> threading.Thread:
    thread = threading.Thread(target=parts.runtime.execute, daemon=True)
    thread.start()
    return thread


def _stop(parts, thread: threading.Thread, agents: _PerTaskAgents) -> None:
    for agent in list(agents.by_task.values()):
        agent.release.set()
    parts.stop_event.set()
    parts.work_signal.signal()
    thread.join(timeout=_DEADLINE_SEC)


def _await(predicate, timeout: float = _DEADLINE_SEC) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- polling is the floor ----------------------------------------------------------------


def test_the_claim_loop_beats_at_the_top_of_every_iteration(tmp_path, monkeypatch) -> None:
    parts, _agents = _runtime(tmp_path, monkeypatch)
    order: list[str] = []
    monkeypatch.setattr(parts.heartbeat, "beat", lambda: order.append("beat"))

    def claim_then_stop() -> None:
        order.append("claim")
        parts.stop_event.set()

    monkeypatch.setattr(parts.runtime._executor, "assign_next", claim_then_stop)

    parts.runtime.execute()
    parts.runtime.drain(deadline_sec=_DEADLINE_SEC)

    # Two beats: one before the executor starts, so a hanging start cannot stay green, and one
    # opening the iteration, so a hanging claim leaves the last beat ageing.
    assert order == ["beat", "beat", "claim"]


def test_task_stop_consumer_is_ready_before_the_runtime_can_claim(tmp_path, monkeypatch) -> None:
    order: list[str] = []

    class Consumer:
        def start(self) -> bool:
            order.append("consumer-ready")
            return True

        def poll(self, timeout: float = 0.0):
            del timeout
            return None

    parts, _agents = _runtime(tmp_path, monkeypatch, task_stops=Consumer())

    def claim_then_stop() -> None:
        order.append("claim")
        parts.stop_event.set()

    monkeypatch.setattr(parts.runtime._executor, "assign_next", claim_then_stop)

    parts.runtime.execute()
    parts.runtime.drain(deadline_sec=_DEADLINE_SEC)

    assert order == ["consumer-ready", "claim"]


def test_an_inprocess_stop_reaches_the_task_without_the_store_poll(tmp_path, monkeypatch) -> None:
    from ouroboros.app.task_stop_notifications import create_task_stop_transport

    publisher, consumer = create_task_stop_transport()
    parts, _agents = _runtime(tmp_path, monkeypatch, task_stops=consumer)
    store = runtime_persistence.initialize()
    agent = _StopCompletingAgent(tmp_path, store)
    parts.runtime._executor.new_agent = lambda: agent  # type: ignore[method-assign]
    _enqueue(tmp_path, store, "stopping")
    thread = _run_in_background(parts)
    try:
        assert agent.entered.wait(_DEADLINE_SEC)
        assert store.tasks_repo.request_task_stop("stopping", scope=TEST_SCOPE)
        started = time.monotonic()

        publisher.publish(TaskStopNotice(runtime_id=parts.runtime.runtime_id, task_ids=("stopping",)))

        assert agent.returned.wait(1.0)
        assert time.monotonic() - started < 1.0
        assert store.tasks_repo.get_task("stopping").status == "cancelled"
    finally:
        _stop(parts, thread, _agents)


def test_a_queued_task_starts_with_no_notification_at_all(tmp_path, monkeypatch) -> None:
    """The wake-up is an optimisation. With a signal that delivers nothing, work still runs."""
    parts, agents = _runtime(tmp_path, monkeypatch, work_signal=NullWorkSignal())
    store = runtime_persistence.initialize()
    _enqueue(tmp_path, store, "t1")

    thread = _run_in_background(parts)
    try:
        assert agents.wait_for("t1")
        assert store.tasks_repo.get_task("t1").status == "running"
    finally:
        _stop(parts, thread, agents)


def test_a_dropped_wake_up_costs_latency_only(tmp_path, monkeypatch) -> None:
    """A signal exists and nobody ever raises it; the poll is what starts the task."""
    parts, agents = _runtime(tmp_path, monkeypatch, poll_interval_sec=0.05)
    store = runtime_persistence.initialize()
    # Enqueued through a handle whose signal nobody listens to: the notification is lost.
    _enqueue(tmp_path, store, "t1")

    thread = _run_in_background(parts)
    try:
        assert agents.wait_for("t1")
    finally:
        _stop(parts, thread, agents)


def test_the_claim_loop_outlives_a_database_outage(tmp_path, monkeypatch, caplog) -> None:
    parts, agents = _runtime(tmp_path, monkeypatch)
    store = runtime_persistence.initialize()
    _enqueue(tmp_path, store, "t1")
    beats: list[str] = []
    monkeypatch.setattr(parts.heartbeat, "beat", lambda: beats.append("beat"))
    with caplog.at_level("WARNING", logger="ouroboros.agent_runtime.runtime"), contextlib.ExitStack() as outage:
        outage.enter_context(refused_connections(store.engine))
        thread = _run_in_background(parts)
        try:
            assert _await(lambda: len(beats) > 2)
            assert not agents.of("t1").entered.is_set()
            outage.close()
            assert agents.wait_for("t1")
        finally:
            _stop(parts, thread, agents)
    assert len([record for record in caplog.records if "Claim failed" in record.getMessage()]) == 1


def test_a_broken_claim_query_still_ends_the_loop(tmp_path, monkeypatch) -> None:
    parts, _agents = _runtime(tmp_path, monkeypatch)

    def rejected_claim() -> None:
        parts.stop_event.set()
        raise ProgrammingError("SELECT", {}, Exception("column does not exist"))

    monkeypatch.setattr(parts.runtime._executor, "assign_next", rejected_claim)

    with pytest.raises(ProgrammingError):
        parts.runtime.execute()
    parts.runtime.drain(deadline_sec=_DEADLINE_SEC)


def test_an_expired_store_deadline_does_not_end_the_loop(tmp_path, monkeypatch) -> None:
    parts, _agents = _runtime(tmp_path, monkeypatch)

    def expired_claim() -> None:
        parts.stop_event.set()
        raise TimeoutError("Runtime store deadline expired")

    monkeypatch.setattr(parts.runtime._executor, "assign_next", expired_claim)

    parts.runtime.execute()
    parts.runtime.drain(deadline_sec=_DEADLINE_SEC)


def test_the_next_task_for_an_owner_starts_without_waiting_out_the_interval(tmp_path, monkeypatch) -> None:
    """Its predecessor closing is exactly when it became claimable, and not before."""
    slow_interval = 30.0
    parts, agents = _runtime(tmp_path, monkeypatch, poll_interval_sec=slow_interval)
    store = runtime_persistence.initialize()
    _enqueue(tmp_path, store, "first")
    _enqueue(tmp_path, store, "second")

    thread = _run_in_background(parts)
    try:
        assert agents.wait_for("first")
        started = time.monotonic()
        agents.of("first").release.set()

        assert agents.wait_for("second")
        assert time.monotonic() - started < slow_interval / 2
    finally:
        _stop(parts, thread, agents)


# --- one owner, one run; several owners, several runs ------------------------------------


def test_two_owners_run_at_once(tmp_path, monkeypatch) -> None:
    parts, agents = _runtime(tmp_path, monkeypatch, slots=2)
    store = runtime_persistence.initialize()
    _enqueue(tmp_path, store, "mine", owner="owner-a")
    _enqueue(tmp_path, store, "theirs", owner="owner-b")

    thread = _run_in_background(parts)
    try:
        assert agents.wait_for("mine")
        assert agents.wait_for("theirs")
    finally:
        _stop(parts, thread, agents)


def test_one_owners_second_task_waits_without_blocking_anyone_else(tmp_path, monkeypatch) -> None:
    parts, agents = _runtime(tmp_path, monkeypatch, slots=3)
    store = runtime_persistence.initialize()
    _enqueue(tmp_path, store, "a-first", owner="owner-a")
    _enqueue(tmp_path, store, "a-second", owner="owner-a")
    _enqueue(tmp_path, store, "c-only", owner="owner-c")

    thread = _run_in_background(parts)
    try:
        assert agents.wait_for("a-first")
        assert agents.wait_for("c-only")
        # The claim's one-run-per-owner predicate hides the second one while the first runs.
        assert not agents.of("a-second").entered.wait(0.5)
        assert store.tasks_repo.get_task("a-second").status in ("queued", "scheduled")
    finally:
        _stop(parts, thread, agents)


def test_a_stuck_run_for_one_owner_does_not_delay_anothers_start(tmp_path, monkeypatch) -> None:
    parts, agents = _runtime(tmp_path, monkeypatch, slots=2)
    store = runtime_persistence.initialize()
    _enqueue(tmp_path, store, "stuck", owner="owner-a")

    thread = _run_in_background(parts)
    try:
        assert agents.wait_for("stuck")
        _enqueue(tmp_path, store, "fresh", owner="owner-b")

        assert agents.wait_for("fresh")
    finally:
        _stop(parts, thread, agents)


def test_the_runtime_keeps_no_per_owner_occupancy_map(tmp_path, monkeypatch) -> None:
    """One owner runs one task because the claim says so, not because anything counts."""
    parts, agents = _runtime(tmp_path, monkeypatch, slots=2)
    store = runtime_persistence.initialize()
    _enqueue(tmp_path, store, "mine", owner="owner-a")

    thread = _run_in_background(parts)
    try:
        assert agents.wait_for("mine")
        state = {**vars(parts.runtime), **vars(parts.runtime._executor)}

        assert not any("owner" in name for name in state)
        assert "owner-a" not in repr(state)
    finally:
        _stop(parts, thread, agents)


def test_a_crashed_loop_does_not_leave_two_stop_listeners(tmp_path, monkeypatch) -> None:
    """A restart must not clear the previous listener's stop event.

    The old listener was given its own Event, so the new listener's creation does
    not un-set the old one — the old thread exits instead of resuming and racing
    the new one on the same consumer.
    """
    import queue as queue_mod

    class _SlowConsumer:
        def __init__(self) -> None:
            self._q: queue_mod.Queue = queue_mod.Queue()
            self.poll_calls = 0

        def start(self) -> bool:
            return True

        def poll(self, timeout: float = 0.0):
            self.poll_calls += 1
            try:
                return self._q.get(timeout=2.0)
            except queue_mod.Empty:
                return None

    consumer = _SlowConsumer()
    parts, _agents = _runtime(tmp_path, monkeypatch, task_stops=consumer)

    parts.runtime._start_task_stop_listener()
    t1 = parts.runtime._task_stop_listener
    assert t1 is not None
    _await(lambda: consumer.poll_calls > 0)

    parts.runtime._stop_task_stop_listener()
    assert t1.is_alive()

    parts.runtime._start_task_stop_listener()
    t2 = parts.runtime._task_stop_listener
    assert t2 is not t1

    _await(lambda: not t1.is_alive(), timeout=5.0)
    assert not t1.is_alive(), "old listener was resurrected after restart"

    parts.runtime._stop_task_stop_listener()
    parts.stop_event.set()


# --- identity, and handing the work back --------------------------------------------------


def test_a_claimed_record_names_the_runtime_that_took_it(tmp_path, monkeypatch) -> None:
    parts, agents = _runtime(tmp_path, monkeypatch)
    store = runtime_persistence.initialize()
    _enqueue(tmp_path, store, "t1")

    thread = _run_in_background(parts)
    try:
        assert agents.wait_for("t1")

        assert store.tasks_repo.get_task("t1").runtime_id == parts.runtime.runtime_id
    finally:
        _stop(parts, thread, agents)


def test_a_departing_runtime_releases_its_own_runs_and_leaves_the_queue_alone(tmp_path, monkeypatch) -> None:
    parts, agents = _runtime(tmp_path, monkeypatch, slots=1)
    store = runtime_persistence.initialize()
    # Claimed first and elsewhere, so the claim below takes this one and no other.
    _enqueue(tmp_path, store, "theirs", owner="owner-c")
    claimed = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    ).claim_next(worker_id=0, runtime_id="another-runtime")
    assert claimed is not None and claimed.task_id == "theirs"
    _enqueue(tmp_path, store, "mine", owner="owner-a")
    _enqueue(tmp_path, store, "queued", owner="owner-b")

    thread = _run_in_background(parts)
    try:
        assert agents.wait_for("mine")
        # Drained while the body is still in flight: what is still held when the deadline
        # passes is what a release has to hand back.
        parts.runtime.drain(deadline_sec=_IN_FLIGHT_DRAIN_SEC)

        assert store.tasks_repo.get_task("mine").error_code == RELEASE_ERROR_CODE
        assert store.tasks_repo.get_task("theirs").status == "running"
        assert store.tasks_repo.get_task("queued").status in ("queued", "scheduled")
    finally:
        _stop(parts, thread, agents)


def test_draining_stops_claiming_before_it_waits(tmp_path, monkeypatch) -> None:
    parts, agents = _runtime(tmp_path, monkeypatch)
    store = runtime_persistence.initialize()
    _enqueue(tmp_path, store, "t1")

    thread = _run_in_background(parts)
    try:
        assert agents.wait_for("t1")
        parts.runtime._executor.begin_drain()
        _enqueue(tmp_path, store, "after-the-drain", owner="owner-b")

        assert not _await(lambda: store.tasks_repo.get_task("after-the-drain").status == "running", timeout=0.5)
    finally:
        _stop(parts, thread, agents)


def test_draining_releases_a_detached_body_before_its_local_stop_can_complete_it(tmp_path, monkeypatch) -> None:
    """The shutdown hand-back, not a stopped graph's empty fallback, owns the outcome."""
    parts, agents = _runtime(tmp_path, monkeypatch)
    store = runtime_persistence.initialize()
    body = _StopCompletingAgent(tmp_path, store)
    parts.runtime._executor.new_agent = lambda: body  # type: ignore[method-assign]
    _enqueue(tmp_path, store, "detached", detached=True)

    thread = _run_in_background(parts)
    try:
        assert body.entered.wait(_DEADLINE_SEC)

        parts.runtime.drain(deadline_sec=_IN_FLIGHT_DRAIN_SEC)

        assert body.returned.wait(_DEADLINE_SEC)
        record = store.tasks_repo.get_task("detached")
        assert record.error_code == RELEASE_ERROR_CODE
        assert record.status != "completed"
        successors = [item for item in store.tasks_repo.list_tasks(statuses=["queued"]) if item.task_id != "detached"]
        assert len(successors) == 1
        assert successors[0].input["instance_release_from"] == "detached"
    finally:
        _stop(parts, thread, agents)


def test_a_run_whose_runtime_vanished_is_still_ended_by_the_supervisors_sweep(tmp_path) -> None:
    """The sweep addresses records by staleness, which is what lets it outlive any runtime."""
    from supervisor.queue import HEARTBEAT_STALE_SEC

    store = runtime_persistence.initialize()
    _enqueue(tmp_path, store, "orphan")
    queue = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    )
    claimed = queue.claim_next(worker_id=0, runtime_id="a-runtime-that-died")
    assert claimed is not None
    claimed.heartbeat_at = _iso_ago(HEARTBEAT_STALE_SEC + 60)
    store.tasks_repo.upsert_task(claimed)

    reclaimed = queue.reclaim_stale_leases()

    assert [record.task_id for record in reclaimed] == ["orphan"]
    assert store.tasks_repo.get_task("orphan").status == "failed"


def _iso_ago(seconds: float) -> str:
    import datetime

    return (datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=seconds)).isoformat()


@pytest.mark.parametrize("signal_kind", ["never delivered", "delivered"])
def test_the_wake_up_changes_latency_and_never_correctness(tmp_path, monkeypatch, signal_kind: str) -> None:
    parts, agents = _runtime(tmp_path, monkeypatch, poll_interval_sec=0.05)
    store = runtime_persistence.initialize()
    _enqueue(tmp_path, store, "t1")
    if signal_kind == "delivered":
        parts.work_signal.signal()

    thread = _run_in_background(parts)
    try:
        assert agents.wait_for("t1")
    finally:
        _stop(parts, thread, agents)
