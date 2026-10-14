"""A run in flight is found through its record, not through this process's memory.

Every test here is about addressing: which instance holds a run, whether its lease is
still current, and whether a stop asked for elsewhere reaches it. None of them needs two
processes, because the whole point of the change is that one process addresses its own
runs the same way it addresses another instance's.
"""

from __future__ import annotations

import datetime
import pathlib
from unittest.mock import Mock

import pytest

from new_core.coordination.inprocess import NullWorkSignal
from ouroboros.agent_events.interfaces import OutboundText, TaskHeartbeat
from ouroboros.storage import task_transitions
from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.queue import TaskQueue
from tests.helpers.agent_events import RecordingAgentEvents
from tests.helpers.agent_owner_session import sql_owner_session_repository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.conversations import make_sql_runtime_persistence
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import (
    TEST_SCOPE,
    make_empty_extension_runtime,
    make_empty_skill_snapshot,
    make_skill_availability,
    scope_with,
)
from tests.helpers.sql_faults import fail_sql
from tests.helpers.startup_usage import sql_startup_usage_repository
from tests.helpers.supervisor import bootstrap_supervisor_for_tests
from tests.helpers.task_stop_state import sql_task_stop_reader
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


def _iso_ago(seconds: float) -> str:
    return (datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=seconds)).isoformat()


def _queue(tmp_path: pathlib.Path, store) -> TaskQueue:
    return TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=600,
        hard_timeout=1800,
        audit_event_repository=sql_audit_event_repository(),
    )


def _second_handle(store):
    """Another handle on the same rows, with its own engine and its own pool.

    Which is all "another instance" ever is: nothing these two share in memory can make a
    write safe, so what stands between them has to be the statement.
    """
    from infra.runtime_database import create_runtime_engine

    return make_sql_runtime_persistence(create_runtime_engine(store.db_urls[0]), db_url=store.db_urls[0])


def _queued(store, task_id: str, *, scope=TEST_SCOPE, detached: bool = False):
    return store.tasks_repo.create_task(
        TaskRequest(
            id=task_id, detached=detached, type="task", text="work", chat_id=int(scope.chat_id), scope=scope.to_dict()
        )
    )


# --- the instance a record names -------------------------------------------


def test_two_runtimes_in_one_process_present_different_identities(tmp_path: pathlib.Path) -> None:
    """The identity is minted per runtime, not derived from the process it runs in.

    A pid or a hostname would make these two equal — and would make a restarted instance
    indistinguishable from the predecessor whose abandoned runs it must not adopt. It is
    the *runtime's*: the supervisor runs nothing and carries none.
    """
    from supervisor.runtime_context import SupervisorRuntime
    from tests.helpers.supervisor import bootstrap_agent_runtime_for_tests

    first = bootstrap_agent_runtime_for_tests(tmp_path / "a").runtime
    second = bootstrap_agent_runtime_for_tests(tmp_path / "b").runtime

    assert first.runtime_id and second.runtime_id
    assert first.runtime_id != second.runtime_id
    assert "runtime_id" not in SupervisorRuntime.__dataclass_fields__


def test_a_claimed_record_names_the_claiming_runtime(tmp_path: pathlib.Path) -> None:
    """And the worker id keeps meaning a lane inside that instance."""
    store = runtime_persistence.initialize()
    _queued(store, "t1")

    claimed = _queue(tmp_path, store).claim_next(worker_id=3, runtime_id="runtime-a")

    assert claimed is not None
    assert claimed.runtime_id == "runtime-a"
    assert claimed.worker_id == 3
    assert store.tasks_repo.get_task("t1").runtime_id == "runtime-a"


def test_two_instances_claiming_onto_lane_zero_do_not_collide(tmp_path: pathlib.Path) -> None:
    """Two owners, because one owner's second record is not claimable while their first runs."""
    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queued(store, "t2", scope=_other_owner_scope())

    first = _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")
    second = _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-b")

    assert first is not None and second is not None
    assert first.worker_id == second.worker_id == 0
    assert {first.runtime_id, second.runtime_id} == {"runtime-a", "runtime-b"}


def test_a_queued_record_names_no_instance(tmp_path: pathlib.Path) -> None:
    """Nothing holds it, so nothing may be read off it as a holder."""
    store = runtime_persistence.initialize()
    record = _queued(store, "t1")

    assert record.runtime_id is None
    assert record.stop_requested_at is None


# --- the lease is written by the run itself --------------------------------


def _bare_agent(tmp_path: pathlib.Path, events=None):
    """Just enough agent for the heartbeat loop: no LLM, no graph, no task."""
    from ouroboros.agent import core

    agent = object.__new__(core.OuroborosAgent)
    agent.env = core.Env(repo_dir=tmp_path, drive_root=tmp_path)
    agent._events = events
    agent._current_task_type = "task"
    return agent


def test_a_running_body_advances_its_own_lease_with_no_dispatch_behind_it(tmp_path: pathlib.Path) -> None:
    """The write is on the body's path: no publisher, no supervisor, still a fresh lease."""
    store = runtime_persistence.initialize()
    _queued(store, "t1")
    claimed = _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")
    assert claimed is not None
    claimed.heartbeat_at = _iso_ago(90)
    store.tasks_repo.upsert_task(claimed)

    _bare_agent(tmp_path, events=None)._beat(TEST_SCOPE, "t1", "running", "trace")

    assert store.tasks_repo.get_task("t1").heartbeat_at > claimed.heartbeat_at


def test_the_heartbeat_loop_survives_a_write_that_raises(tmp_path: pathlib.Path, monkeypatch) -> None:
    """A transient database error costs a log line, not four beats and a reclaim."""
    import time

    from ouroboros.agent import core

    def _boom(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(runtime_persistence.initialize().tasks_repo, "heartbeat_task", _boom)
    monkeypatch.setattr(core, "HEARTBEAT_PERIOD_SEC", 0.01)
    events = RecordingAgentEvents()
    agent = _bare_agent(tmp_path, events=events)

    heartbeat = agent._start_task_heartbeat_loop("t1", TEST_SCOPE, {})
    assert heartbeat is not None
    stop, thread = heartbeat
    deadline = time.monotonic() + 2.0
    while len(events.published) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=core._HEARTBEAT_JOIN_TIMEOUT_SEC)

    assert len(events.published) >= 3


def test_a_heartbeat_in_any_status_still_reaches_the_live_log(tmp_path: pathlib.Path) -> None:
    """The forward is observability and never asked the store whether it applied."""
    from types import SimpleNamespace

    from supervisor.events import _handle_task_heartbeat

    pushed: list[dict] = []
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, bridge=SimpleNamespace(push_log=pushed.append))

    _handle_task_heartbeat(
        TaskHeartbeat(scope=TEST_SCOPE, task_id="never-stored", task_type="task", phase="running"),
        ctx,
    )

    assert pushed == [
        {
            "ts": pushed[0]["ts"],
            "type": "task_heartbeat",
            "task_id": "never-stored",
            "task_type": "task",
            "phase": "running",
        }
    ]


# --- the stop intent -------------------------------------------------------


def _other_owner_scope():
    from tests.helpers.shared import scope_with

    return scope_with(TEST_SCOPE, owner_id="someone-else")


def test_a_stop_applies_to_the_owner_s_own_running_record(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")

    assert store.tasks_repo.request_task_stop("t1", scope=TEST_SCOPE) is True
    assert store.tasks_repo.get_task("t1").stop_requested_at


def test_a_stop_for_another_owner_s_record_applies_nothing(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")

    assert store.tasks_repo.request_task_stop("t1", scope=_other_owner_scope()) is False
    assert store.tasks_repo.get_task("t1").stop_requested_at is None


def test_a_stop_for_a_record_that_is_no_longer_running_applies_nothing(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")
    store.tasks_repo.complete_task("t1", result="done")

    assert store.tasks_repo.request_task_stop("t1", scope=TEST_SCOPE) is False
    assert store.tasks_repo.get_task("t1").stop_requested_at is None


def test_a_second_stop_leaves_the_first_request_s_time_in_place(tmp_path: pathlib.Path) -> None:
    """The grace period runs from when stopping was first asked for."""
    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")

    assert store.tasks_repo.request_task_stop("t1", scope=TEST_SCOPE) is True
    first = store.tasks_repo.get_task("t1").stop_requested_at
    assert store.tasks_repo.request_task_stop("t1", scope=TEST_SCOPE) is False
    assert store.tasks_repo.get_task("t1").stop_requested_at == first


def test_a_round_trip_through_the_store_does_not_erase_a_recorded_stop(tmp_path: pathlib.Path) -> None:
    """A heartbeat is a read-modify-write; the stop column is not one of the fields it writes."""
    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")
    store.tasks_repo.request_task_stop("t1", scope=TEST_SCOPE)

    store.tasks_repo.heartbeat_task("t1", phase="running")

    assert store.tasks_repo.get_task("t1").stop_requested_at
    assert sql_task_stop_reader(store.engine).read_task_stop_state("t1").stop_requested is True


def test_the_stop_state_reads_both_facts(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")

    running = sql_task_stop_reader(store.engine).read_task_stop_state("t1")
    assert (running.stop_requested, running.is_terminal) == (False, False)

    store.tasks_repo.cancel_task("t1")
    closed = sql_task_stop_reader(store.engine).read_task_stop_state("t1")
    assert (closed.stop_requested, closed.is_terminal) == (False, True)

    gone = sql_task_stop_reader(store.engine).read_task_stop_state("never-existed")
    assert (gone.stop_requested, gone.is_terminal) == (False, True)


def test_a_listing_narrowed_to_a_scope_shows_only_that_owner_s_records(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    other = _other_owner_scope()
    _queued(store, "mine")
    _queued(store, "theirs", scope=other)

    assert [r.task_id for r in store.tasks_repo.list_tasks(statuses=["queued"], scope=TEST_SCOPE)] == ["mine"]
    assert [r.task_id for r in store.tasks_repo.list_tasks(statuses=["queued"], scope=other)] == ["theirs"]
    assert {r.task_id for r in store.tasks_repo.list_tasks(statuses=["queued"])} == {"mine", "theirs"}


# --- the signal reads shared state -----------------------------------------


def _store_signal(store, task_id: str = "t1", *, interval: float = 60.0, scope=TEST_SCOPE, tasks_repo=None):
    from ouroboros.agent_runtime.cancellation import StoreCancellation

    return StoreCancellation(
        tasks_repo if tasks_repo is not None else store.tasks_repo,
        task_id=task_id,
        scope=scope,
        poll_interval_sec=interval,
    )


def test_a_stop_recorded_by_another_store_handle_is_observed(tmp_path: pathlib.Path) -> None:
    """ "Another instance" is another handle on the same rows — which is all it ever is."""
    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")
    signal = _store_signal(store, interval=0.0)
    assert signal.is_stopped() is False

    elsewhere = _second_handle(store)
    assert elsewhere.tasks_repo.request_task_stop("t1", scope=TEST_SCOPE) is True

    assert signal.is_stopped() is True


def test_many_checks_inside_one_interval_cost_one_read(tmp_path: pathlib.Path) -> None:
    """A run's cost must not depend on how long it has been waiting to be stopped."""
    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")
    reader = Mock(wraps=sql_task_stop_reader(store.engine))
    signal = _store_signal(store, interval=60.0, tasks_repo=reader)

    for _ in range(20):
        assert signal.is_stopped() is False

    reader.read_task_stop_state.assert_called_once_with("t1")


def test_a_local_request_stop_is_observed_immediately(tmp_path: pathlib.Path) -> None:
    """No waiting out the interval for a stop this process asked for itself."""
    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")
    signal = _store_signal(store, interval=3600.0)
    assert signal.is_stopped() is False

    signal.request_stop()

    assert signal.is_stopped() is True
    assert store.tasks_repo.get_task("t1").stop_requested_at


def test_a_terminal_record_stops_its_run(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")
    signal = _store_signal(store, interval=0.0)
    assert signal.is_stopped() is False

    store.tasks_repo.fail_task("t1", result="killed elsewhere")

    assert signal.is_stopped() is True


def test_a_store_read_that_fails_is_not_a_stop(tmp_path: pathlib.Path) -> None:
    from sqlalchemy.exc import OperationalError

    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")

    reader = sql_task_stop_reader()
    with fail_sql("FROM tasks", OperationalError("select", {}, Exception("database is locked"))):
        assert _store_signal(store, interval=0.0, tasks_repo=reader).is_stopped() is False


def test_the_composed_signal_answers_from_whichever_part_stopped(tmp_path: pathlib.Path) -> None:
    from ouroboros.agent_runtime.cancellation import CompositeCancellation, EventCancellation

    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")
    event = EventCancellation()
    composed = CompositeCancellation(event, _store_signal(store, interval=0.0))
    assert composed.is_stopped() is False

    event.request_stop()

    assert composed.is_stopped() is True


def test_the_body_is_handed_a_signal_that_reads_the_record(tmp_path: pathlib.Path, monkeypatch) -> None:
    """What `_assign_slot` submits, not what the slot holds: the slot's event is one part."""
    from ouroboros.agent_runtime import executor as workers_mod
    from ouroboros.agent_runtime.executor import TaskExecutor

    # The interval is the stated bound on how late a remote stop may be observed; the test
    # is about the composition, so it does not wait one out.
    monkeypatch.setattr(workers_mod, "STOP_POLL_INTERVAL_SEC", 0.0)

    store = runtime_persistence.initialize()
    executor = TaskExecutor(
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
    executor.ensure_started()
    executor.task_queue.enqueue_dict(
        {"id": "t1", "type": "task", "text": "go", "chat_id": 1, "scope": TEST_SCOPE.to_dict()}
    )
    handed: list = []
    executor._run_task = lambda _record, cancellation: handed.append(cancellation)  # type: ignore[method-assign]
    slot = executor._slots[0]

    assert executor._assign_slot(slot) is not None
    slot.future.result(timeout=5)
    signal = handed[0]
    assert signal.is_stopped() is False

    store.tasks_repo.request_task_stop("t1", scope=TEST_SCOPE)

    assert signal.is_stopped() is True


def test_a_run_stopped_from_elsewhere_ends_with_no_final_content(tmp_path: pathlib.Path) -> None:
    """Through the loop: the guard reads the signal, so a remote stop ends the run as a local one does."""
    from types import SimpleNamespace

    from tests.helpers.factories import make_mock_llm
    from tests.helpers.loop_harness import drive_loop

    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")
    store.tasks_repo.request_task_stop("t1", scope=TEST_SCOPE)
    llm = make_mock_llm(model="m1")

    text, _usage, _trace = drive_loop(
        messages=[{"role": "user", "content": "do it"}],
        tool_ctx=SimpleNamespace(_pending_compaction=None, tool_surface=None),
        llm=llm,
        drive_logs=tmp_path / "logs",
        drive_root=tmp_path,
        task_id="t1",
        cancellation=_store_signal(store, interval=0.0),
    )

    assert text == ""
    # The stop is checked before the first round, so no answer was ever asked for.
    assert [call for call in llm.mock_calls if not call[0].startswith("default_model")] == []


# --- addressing replaces slot pokes ----------------------------------------


def test_a_cancel_refuses_another_owner_s_task(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    other = _other_owner_scope()
    _queued(store, "theirs", scope=other)
    queue = _queue(tmp_path, store)

    assert queue.cancel_task_by_id("theirs", scope=TEST_SCOPE) is False
    assert store.tasks_repo.get_task("theirs").status == "queued"
    assert queue.cancel_task_by_id("theirs", scope=other) is True
    assert store.tasks_repo.get_task("theirs").status == "cancelled"


def test_a_cancel_of_a_run_held_elsewhere_closes_the_record(tmp_path: pathlib.Path) -> None:
    """No slot of this process holds it, and the cancel still lands — through the record."""
    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-elsewhere")

    assert _queue(tmp_path, store).cancel_task_by_id("t1", scope=TEST_SCOPE)

    assert store.tasks_repo.get_task("t1").status == "cancelled"


def test_a_cancel_of_a_task_that_never_existed_is_refused(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()

    assert _queue(tmp_path, store).cancel_task_by_id("nope", scope=TEST_SCOPE) is False


def test_the_status_report_names_only_the_asking_owner_s_tasks(tmp_path: pathlib.Path) -> None:
    from supervisor.state import status_text

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    store = runtime_persistence.current
    other = _other_owner_scope()
    _queued(store, "mine")
    _queued(store, "theirs", scope=other)
    runtime.task_queue.claim_next(worker_id=0, runtime_id="runtime-a")
    runtime.task_queue.claim_next(worker_id=0, runtime_id="runtime-a")

    report = status_text(
        600,
        1800,
        scope=TEST_SCOPE,
        tool_confirmation_reader=sql_tool_confirmation_reader(),
    )

    assert "mine" in report
    assert "theirs" not in report


def test_the_status_report_names_the_question_the_owner_has_not_answered(tmp_path: pathlib.Path) -> None:
    """A parked run is in neither list, so without this line a waiting question leaves no trace."""
    from ouroboros.storage.runtime_models import ToolConfirmation
    from supervisor.state import status_text

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    store = runtime_persistence.current
    _queued(store, "mine")
    runtime.task_queue.claim_next(worker_id=0, runtime_id="runtime-a")
    sql_tool_confirmation_reader(store.engine).park_and_open(
        task_id="mine",
        claim_token="",
        confirmation=ToolConfirmation(
            owner_id=TEST_SCOPE.owner_id,
            agent_id=TEST_SCOPE.agent_id,
            surface_id=str(TEST_SCOPE.surface_id),
            chat_id=TEST_SCOPE.chat_id,
            confirmation_id="ab12cd34ef56",
            task_id="mine",
            tool_name="data_write",
        ),
        snapshot={"round_idx": 1},
        arguments={},
        workspace="",
    )

    report = status_text(
        600,
        1800,
        scope=TEST_SCOPE,
        tool_confirmation_reader=runtime.tool_confirmation_reader,
    )

    assert "awaiting_confirmation: data_write [ab12cd34] task=mine" in report


# --- a stale lease is reclaimed exactly once -------------------------------


def _stale_running(
    store,
    tmp_path: pathlib.Path,
    task_id: str,
    *,
    runtime_id: str = "runtime-a",
    attempt: int = 1,
    scope=TEST_SCOPE,
    detached: bool = False,
):
    """A record claimed by a named instance whose lease then went quiet."""
    from supervisor.queue import HEARTBEAT_STALE_SEC

    _queued(store, task_id, scope=scope, detached=detached)
    claimed = _queue(tmp_path, store).claim_next(worker_id=0, runtime_id=runtime_id)
    assert claimed is not None
    claimed.heartbeat_at = _iso_ago(HEARTBEAT_STALE_SEC + 60)
    claimed.started_at = claimed.heartbeat_at
    claimed.attempt = attempt
    return store.tasks_repo.upsert_task(claimed)


@pytest.mark.parametrize("outcome", ["committed", "absent", "conflict", "unavailable"])
def test_successor_unknown_commit_recovers_persisted_lineage_before_retry(tmp_path, monkeypatch, outcome) -> None:
    store = runtime_persistence.initialize()
    predecessor = _stale_running(store, tmp_path, "t1", detached=True)
    queue = _queue(tmp_path, store)
    request = {"task_input": predecessor.to_agent_input(), "task_id": "t1", "new_attempt": 2, "marker": "lease_reclaim"}
    successor = queue._retry_request(**request)
    assert successor.id == _queue(tmp_path, _second_handle(store))._retry_request(**request).id
    commit, get_task = store.engine.dialect.do_commit, store.tasks_repo.get_task
    attempts, reads = [], []

    def lost_commit(conn):
        monkeypatch.setattr(store.engine.dialect, "do_commit", commit)
        if outcome in {"absent", "conflict"}:
            conn.rollback()
        else:
            commit(conn)
        if outcome == "conflict":
            _second_handle(store).tasks_repo.transition_running(
                "t1",
                cutoff_iso="9999",
                result="winner",
                kind="lease_lost",
                attempt=2,
                successor=successor.model_copy(update={"id": "winner"}),
            )
        raise OSError("commit acknowledgement lost")

    def reread(task_id):
        reads.append(task_id)
        if outcome == "unavailable":
            raise OSError("reread unavailable")
        return get_task(task_id)

    insert = store.tasks_repo._insert_task

    def inserting(conn, task):
        attempts.append(task.task_id)
        insert(conn, task)

    monkeypatch.setattr(store.tasks_repo, "_insert_task", inserting)
    monkeypatch.setattr(store.tasks_repo, "get_task", reread)
    monkeypatch.setattr(store.engine.dialect, "do_commit", lost_commit)
    if outcome in {"absent", "unavailable"}:
        with pytest.raises(OSError):
            queue.reclaim_stale_leases()
    else:
        assert [r.task_id for r in queue.reclaim_stale_leases()] == ([] if outcome == "conflict" else ["t1"])
    assert reads and attempts == [successor.id]
    monkeypatch.setattr(store.tasks_repo, "get_task", get_task)
    queue.reclaim_stale_leases()
    assert attempts == [successor.id] * (2 if outcome == "absent" else 1)
    original, current = get_task("t1"), store.tasks_repo.get_current_job("t1", scope=TEST_SCOPE)
    assert (original.status, original.error_code, original.attempt) == ("failed", "lease_lost", 2)
    assert (current.task_id, current.lineage_ordinal, current.attempt) == (
        "winner" if outcome == "conflict" else successor.id,
        1,
        2,
    )
    if outcome == "conflict":
        assert original.result == "winner"
    assert current.input["lease_reclaim_from"] == "t1"
    assert len(store.tasks_repo.list_tasks()) == 2


def test_stale_lease_does_not_retry_a_stopped_detached_job(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _stale_running(store, tmp_path, "stopped-job", detached=True)
    store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="stopped-job")

    _queue(tmp_path, store).reclaim_stale_leases()

    assert store.tasks_repo.get_task("stopped-job").status == "cancelled"
    assert store.tasks_repo.list_tasks(statuses=["queued"]) == []


def test_a_chain_that_has_spent_its_attempts_is_not_cloned_again(tmp_path: pathlib.Path) -> None:
    from supervisor.queue import QUEUE_MAX_RETRIES

    store = runtime_persistence.initialize()
    _stale_running(store, tmp_path, "t1", attempt=QUEUE_MAX_RETRIES + 1)

    _queue(tmp_path, store).reclaim_stale_leases()

    assert store.tasks_repo.get_task("t1").status == "failed"
    assert [r.task_id for r in store.tasks_repo.list_tasks(statuses=["queued"])] == []


def test_a_live_heartbeating_run_is_untouched(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _queued(store, "t1")
    _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-a")

    assert _queue(tmp_path, store).reclaim_stale_leases() == []
    assert store.tasks_repo.get_task("t1").status == "running"


def test_a_record_with_no_instance_is_skipped(tmp_path: pathlib.Path) -> None:
    """A row written by an instance that never wrote leases: its silence says nothing."""
    from ouroboros.storage.runtime_models import TaskRequest
    from supervisor.queue import HEARTBEAT_STALE_SEC

    store = runtime_persistence.initialize()
    record = store.tasks_repo.create_task(
        TaskRequest(id="legacy", type="task", text="work", chat_id=1, scope=TEST_SCOPE.to_dict()),
        status="running",
    )
    record.heartbeat_at = _iso_ago(HEARTBEAT_STALE_SEC + 60)
    store.tasks_repo.upsert_task(record)
    assert store.tasks_repo.get_task("legacy").runtime_id is None

    assert _queue(tmp_path, store).reclaim_stale_leases() == []
    assert store.tasks_repo.get_task("legacy").status == "running"


def test_a_reclaim_stops_a_run_it_does_not_hold_through_that_runs_own_record(tmp_path: pathlib.Path) -> None:
    """The sweep touches no lane, here or anywhere: it closes records, and a body reads its own."""
    from ouroboros.agent_runtime.cancellation import StoreCancellation

    store = runtime_persistence.initialize()
    _stale_running(store, tmp_path, "mine", runtime_id="runtime-here")
    _stale_running(store, tmp_path, "theirs", runtime_id="runtime-elsewhere", scope=_other_owner_scope())
    elsewhere = StoreCancellation(
        store.tasks_repo,
        task_id="theirs",
        scope=_other_owner_scope(),
        poll_interval_sec=0.0,
    )
    assert elsewhere.is_stopped() is False

    _queue(tmp_path, store).reclaim_stale_leases()

    assert store.tasks_repo.get_task("theirs").status == "failed"
    assert elsewhere.is_stopped() is True


def test_a_late_answer_from_the_reclaimed_original_leaves_the_clone_alone(tmp_path: pathlib.Path) -> None:
    """At-least-once, written down: the zombie's answer is late, not a fresh reply."""
    from types import SimpleNamespace

    from ouroboros.agent.task_pipeline import emit_task_results

    store = runtime_persistence.initialize()
    _stale_running(store, tmp_path, "t1")
    _queue(tmp_path, store).reclaim_stale_leases()
    reclaimed_at = store.tasks_repo.get_task("t1").finished_at
    clone = next(r for r in store.tasks_repo.list_tasks(statuses=["queued"]) if r.task_id != "t1")
    store.tasks_repo.complete_task(clone.task_id, result="the clone's answer")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    pending: list = []
    emit_task_results(
        SimpleNamespace(drive_root=tmp_path),
        object(),
        pending,
        {"id": "t1", "type": "task", "chat_id": 1, "text": "work"},
        "the original's answer",
        {"rounds": 3},
        {"tool_calls": []},
        0.0,
        tmp_path / "logs",
        scope=TEST_SCOPE,
        has_answer=True,
        stopped_externally=True,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )

    original = store.tasks_repo.get_task("t1")
    assert (original.status, original.finished_at) == ("failed", reclaimed_at)
    assert "stopped reporting" in original.result
    assert store.tasks_repo.get_task(clone.task_id).result == "the clone's answer"
    late = [event for event in pending if isinstance(event, OutboundText)]
    assert len(late) == 1
    assert late[0].text.startswith("⏳ Late result")


# --- the departure half of the lease ----------------------------------------


def _running_on(
    store,
    tmp_path: pathlib.Path,
    task_id: str,
    *,
    runtime_id: str = "runtime-a",
    attempt: int = 1,
    scope=TEST_SCOPE,
):
    """A record a named instance claimed and is still holding.

    The scope is a parameter because one owner has one running root run: a second record
    for the same owner is not claimable while the first is running, which is the rule
    rather than an accident of these tests.
    """
    _queued(store, task_id, scope=scope)
    claimed = _queue(tmp_path, store).claim_next(worker_id=0, runtime_id=runtime_id)
    assert claimed is not None and claimed.task_id == task_id
    claimed.attempt = attempt
    return store.tasks_repo.upsert_task(claimed)


def test_a_release_hands_back_the_runs_this_instance_holds(tmp_path: pathlib.Path) -> None:
    from supervisor.queue import RELEASE_ERROR_CODE

    store = runtime_persistence.initialize()
    _running_on(store, tmp_path, "t1")

    released = _queue(tmp_path, store).release_own_runs(runtime_id="runtime-a")

    assert [record.task_id for record in released] == ["t1"]
    original = store.tasks_repo.get_task("t1")
    assert original.error_code == RELEASE_ERROR_CODE
    assert original.status not in ("running", "failed")
    # A deploy is not one of the task's tries, so the hand-back spends no attempt.
    assert original.attempt == 1
    clone = [r for r in store.tasks_repo.list_tasks(statuses=["queued"]) if r.task_id != "t1"]
    assert len(clone) == 1
    assert clone[0].input["instance_release_from"] == "t1"
    assert clone[0].attempt == 1


def test_a_release_finds_its_run_beyond_the_default_listing_batch(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    other_scope = _other_owner_scope()
    for index in range(501):
        store.tasks_repo.create_task(
            TaskRequest(
                id=f"other-{index:03d}",
                text="work",
                scope=scope_with(other_scope, owner_id=f"other-{index}").to_dict(),
            ),
            status="running",
        )
    _running_on(store, tmp_path, "mine")

    released = _queue(tmp_path, store).release_own_runs(runtime_id="runtime-a")

    assert [record.task_id for record in released] == ["mine"]


def test_a_release_does_not_spend_the_failure_retry_ceiling(tmp_path: pathlib.Path) -> None:
    """Two ordinary deploys must not close a long run as `cancelled`.

    The regression this pins: the hand-back used to increment `attempt` against
    `QUEUE_MAX_RETRIES`, so the second instance to depart found the ceiling spent and
    stopped the task instead of handing it on.
    """
    from supervisor.queue import QUEUE_MAX_RETRIES

    store = runtime_persistence.initialize()
    _queued(store, "t1")
    for hop in range(QUEUE_MAX_RETRIES + 2):
        queue = _queue(tmp_path, store)
        claimed = queue.claim_next(worker_id=0, runtime_id=f"runtime-{hop}")
        assert claimed is not None, f"hop {hop} left nothing claimable"
        claimed.input["instance_release_hops"] = 5
        store.tasks_repo.upsert_task(claimed)
        released = queue.release_own_runs(runtime_id=f"runtime-{hop}")
        assert [record.task_id for record in released] == [claimed.task_id]
        clones = store.tasks_repo.list_tasks(statuses=["queued"])
        assert len(clones) == 1, f"hop {hop} stopped the chain"
        # The failure ceiling is untouched across every graceful handoff.
        assert clones[0].attempt == 1


@pytest.mark.parametrize("authority", ["live", "expired", "foreign", "missing", "foreign_runtime"])
def test_release_requires_live_holder_and_expiry_uses_lease_loss_recovery(tmp_path, authority):
    from sqlalchemy import text

    store = runtime_persistence.initialize()
    _queued(store, "job", detached=True)
    held = store.tasks_repo.claim_next_task(runtime_id="runtime-a")
    token = held.metadata["execution_claim"]
    if authority == "expired":
        with store.engine.begin() as conn:
            conn.execute(text("UPDATE tasks SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE task_id='job'"))
    queue = _queue(tmp_path, store)
    successor = queue._retry_request(held.to_agent_input(), task_id="job", new_attempt=1, marker="instance_release")
    supplied = "foreign" if authority == "foreign" else "" if authority == "missing" else token
    result = store.tasks_repo.transition_running(
        "job",
        runtime_id="runtime-b" if authority == "foreign_runtime" else "runtime-a",
        claim_token=supplied,
        result="release",
        kind="instance_released",
        attempt=1,
        successor=successor,
    )
    assert (result is not None) is (authority == "live")
    if authority == "expired":
        assert store.tasks_repo.get_task("job").status == "running"
        queue.reclaim_stale_leases()
        assert store.tasks_repo.get_task("job").error_code == "lease_lost"
        assert store.tasks_repo.get_current_job("job", scope=TEST_SCOPE).attempt == 2
    elif authority == "live":
        assert result[0].error_code == "instance_released"
        assert store.tasks_repo.get_current_job("job", scope=TEST_SCOPE).attempt == 1
    else:
        assert store.tasks_repo.get_task("job").status == "running"
        assert len(store.tasks_repo.list_tasks()) == 1


def test_a_release_leaves_another_instances_run_alone(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _running_on(store, tmp_path, "mine", runtime_id="runtime-a")
    _running_on(store, tmp_path, "theirs", runtime_id="runtime-b", scope=_other_owner_scope())

    released = _queue(tmp_path, store).release_own_runs(runtime_id="runtime-a")

    assert [record.task_id for record in released] == ["mine"]
    assert store.tasks_repo.get_task("theirs").status == "running"


def test_a_release_does_not_resurrect_a_record_reclaimed_in_between(tmp_path: pathlib.Path) -> None:
    """The listing found it; the write names the instance again, and by then it is not ours."""
    from supervisor.queue import HEARTBEAT_STALE_SEC

    store = runtime_persistence.initialize()
    held = _running_on(store, tmp_path, "t1")
    held.heartbeat_at = _iso_ago(HEARTBEAT_STALE_SEC + 60)
    store.tasks_repo.upsert_task(held)
    _queue(tmp_path, store).reclaim_stale_leases()

    applied = store.tasks_repo.transition_running(
        "t1",
        runtime_id="runtime-a",
        result="released",
        kind="instance_released",
        attempt=9,
    )

    assert applied is None
    reclaimed = store.tasks_repo.get_task("t1")
    assert reclaimed.status == "failed"
    assert reclaimed.attempt == 2


def _executor(tmp_path: pathlib.Path, store, *, runtime_id: str = "runtime-a"):
    from ouroboros.agent_runtime.executor import TaskExecutor

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
        runtime_id=runtime_id,
        work_signal=NullWorkSignal(),
        execution_memory_repository=sql_execution_memory_repository(),
        tool_confirmation_reader=sql_tool_confirmation_reader(),
        audit_event_repository=sql_audit_event_repository(),
    )


def test_a_shutdown_closes_only_the_departing_instances_runs(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _running_on(store, tmp_path, "mine", runtime_id="runtime-a")
    _running_on(store, tmp_path, "theirs", runtime_id="runtime-b", scope=_other_owner_scope())
    _queued(store, "waiting")
    executor = _executor(tmp_path, store, runtime_id="runtime-a")
    executor.ensure_started()

    executor.shutdown()

    assert store.tasks_repo.get_task("mine").status != "running"
    assert store.tasks_repo.get_task("theirs").status == "running"
    assert store.tasks_repo.get_task("waiting").status == "queued"


def test_a_shutdown_leaves_the_queue_claimable(tmp_path: pathlib.Path) -> None:
    """The backlog survives a restart instead of being emptied by whoever left."""
    store = runtime_persistence.initialize()
    _queued(store, "waiting")
    executor = _executor(tmp_path, store, runtime_id="runtime-a")
    executor.ensure_started()

    executor.shutdown()

    later = _queue(tmp_path, store).claim_next(worker_id=0, runtime_id="runtime-b")
    assert later is not None and later.task_id == "waiting"


# --- the drain --------------------------------------------------------------


def _held_slot(executor, task_id: str):
    """The executor's lane, holding a body that has not returned."""
    from concurrent.futures import Future

    slot = executor.get_slot(0)
    assert slot is not None
    slot.busy_task_id = task_id
    slot.future = Future()
    return slot


@pytest.mark.parametrize("close", ["begin_drain", "shutdown"])
def test_a_drain_stops_claiming_before_it_waits(tmp_path: pathlib.Path, close) -> None:
    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    _queued(store, "waiting")

    getattr(executor, close)()
    executor.assign_next()

    assert store.tasks_repo.get_task("waiting").status == "queued"


@pytest.mark.parametrize("blocked", ["claim", "release", "failed_future"])
def test_drain_bounds_inflight_database_work_and_forbids_late_commit(tmp_path, monkeypatch, blocked):
    import threading
    import time

    store = runtime_persistence.initialize()
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    _queued(store, "job", detached=True)
    if blocked != "claim":
        claimed = store.tasks_repo.claim_next_task(runtime_id="runtime-a")
        slot = _held_slot(executor, "job")
        slot.claim_token = claimed.metadata["execution_claim"]
        if blocked == "failed_future":
            slot.future.set_exception(RuntimeError("body failed"))
    entered, resume = threading.Event(), threading.Event()
    method = "get_task" if blocked == "failed_future" else "database_now"
    target = store.tasks_repo if blocked == "failed_future" else task_transitions
    clock = getattr(target, method)

    def delayed_clock(conn):
        entered.set()
        resume.wait(2)
        return clock(conn)

    monkeypatch.setattr(target, method, delayed_clock)
    ran = []
    executor._run_task = lambda record, stop: ran.append(record.task_id)
    assigner = threading.Thread(target=executor.assign_next)
    if blocked == "claim":
        assigner.start()
        assert entered.wait(1)
    safety = threading.Timer(0.8, resume.set)
    safety.start()
    # The one case that needs the release to commit: 0.15s loses to a stalled runner.
    deadline_sec = 1.0 if blocked == "failed_future" else 0.15
    start = time.monotonic()
    try:
        executor.drain(deadline_sec=deadline_sec)
        assert time.monotonic() - start < deadline_sec + 0.35
    finally:
        resume.set()
        safety.cancel()
        if blocked == "claim":
            assigner.join(2)
    assert ran == []
    assert (
        store.tasks_repo.get_task("job").status
        == {"claim": "queued", "release": "running", "failed_future": "cancelled"}[blocked]
    )
    assert len(store.tasks_repo.list_tasks()) == (2 if blocked == "failed_future" else 1)


def test_a_body_that_returns_during_the_drain_keeps_its_own_outcome(tmp_path: pathlib.Path) -> None:
    """It closed its own record and published its answer; the release must not undo that."""
    import threading

    store = runtime_persistence.initialize()
    _running_on(store, tmp_path, "t1")
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    slot = _held_slot(executor, "t1")

    def _finish() -> None:
        assert not slot.cancellation.is_stopped()
        store.tasks_repo.complete_task("t1", result="the answer")
        slot.future.set_result(None)

    threading.Timer(0.05, _finish).start()
    executor.drain(deadline_sec=10.0)

    record = store.tasks_repo.get_task("t1")
    assert (record.status, record.result) == ("completed", "the answer")
    assert [r.task_id for r in store.tasks_repo.list_tasks(statuses=["queued"])] == []


def test_a_body_that_overruns_the_drain_has_its_record_released(tmp_path: pathlib.Path) -> None:
    from supervisor.queue import RELEASE_ERROR_CODE

    store = runtime_persistence.initialize()
    _running_on(store, tmp_path, "t1")
    executor = _executor(tmp_path, store)
    executor.ensure_started()
    slot = _held_slot(executor, "t1")

    executor.drain(deadline_sec=5.0)

    record = store.tasks_repo.get_task("t1")
    assert record.status != "running"
    assert record.error_code == RELEASE_ERROR_CODE
    assert slot.busy_task_id is None
    clone = [r for r in store.tasks_repo.list_tasks(statuses=["queued"]) if r.task_id != "t1"]
    assert len(clone) == 1
    assert clone[0].input["instance_release_from"] == "t1"


# --- the invariant, pinned -------------------------------------------------


def test_no_production_path_indexes_a_worker_slot_by_position(tmp_path: pathlib.Path) -> None:
    """A lane number is not an address: every instance has a lane `0`.

    The defect this change removes — `executor.get_slot(0)` standing in for "the run the
    owner means" — is the kind that comes back as a convenience, so the shape is pinned
    rather than only the behaviour. A lane read *off a record* is fine: that is the record
    saying which lane of this process holds the run.
    """
    import re

    from ouroboros.agent_runtime.executor import TaskExecutor

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    roots = [repo_root / "supervisor", repo_root / "ouroboros", repo_root / "transport"]
    offenders: list[str] = []
    for root in roots:
        for source_file in root.rglob("*.py"):
            source = source_file.read_text(encoding="utf-8")
            if re.search(r"get_slot\(\s*\d", source):
                offenders.append(f"{source_file.relative_to(repo_root)}: indexes a worker slot by a lane literal")
            if "get_slot_for_task" in source:
                offenders.append(f"{source_file.relative_to(repo_root)}: searches the slot map for a run")
    assert offenders == []
    # And the accessor that made the search easy is gone from the executor itself.
    assert not hasattr(TaskExecutor, "get_slot_for_task")
