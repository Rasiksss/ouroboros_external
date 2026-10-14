"""Tests for zombie task prevention with store-backed queue state."""

from __future__ import annotations

import logging

from new_core.coordination.inprocess import NullWorkSignal
from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.task_results import STATUS_COMPLETED, STATUS_FAILED, load_task_result
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.runtime_store import claim_task, park_run
from tests.helpers.shared import TEST_SCOPE, scope_with
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


def test_write_failure_result_creates_store_record(tmp_path) -> None:
    from tests.helpers.supervisor import bootstrap_agent_runtime_for_tests

    executor = bootstrap_agent_runtime_for_tests(tmp_path).runtime._executor
    executor._write_failure_result("abc123", scope=TEST_SCOPE)

    loaded = load_task_result(tmp_path, "abc123")
    assert loaded is not None
    assert loaded["task_id"] == "abc123"
    assert loaded["status"] == STATUS_FAILED
    assert isinstance(loaded["result"], str) and len(loaded["result"]) > 0


def test_write_failure_result_does_not_overwrite_terminal(tmp_path) -> None:
    from ouroboros.storage.task_results import write_task_result
    from tests.helpers.supervisor import bootstrap_agent_runtime_for_tests

    executor = bootstrap_agent_runtime_for_tests(tmp_path).runtime._executor
    write_task_result(tmp_path, "xyz789", STATUS_COMPLETED, scope=TEST_SCOPE, result="Success!")
    executor._write_failure_result("xyz789")

    loaded = load_task_result(tmp_path, "xyz789")
    assert loaded is not None
    assert loaded["status"] == STATUS_COMPLETED
    assert loaded["result"] == "Success!"


def test_unauthorized_failure_preserves_detached_stop_intent_for_recovery(tmp_path) -> None:
    from tests.helpers.supervisor import bootstrap_agent_runtime_for_tests

    executor = bootstrap_agent_runtime_for_tests(tmp_path).runtime._executor
    store = runtime_persistence.current
    store.tasks_repo.create_task(TaskRequest(id="stopped-job", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    assert store.tasks_repo.claim_next_task(runtime_id="runtime") is not None
    store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="stopped-job")

    executor._write_failure_result("stopped-job", reason="body crashed after stop")

    stopped = store.tasks_repo.get_task("stopped-job")
    assert stopped.status == "running" and stopped.stop_requested_at
    stopped.heartbeat_at = "2000-01-01T00:00:00+00:00"
    store.tasks_repo.upsert_task(stopped)
    executor.task_queue.reclaim_stale_leases()
    assert store.tasks_repo.get_task("stopped-job").status == "cancelled"


def test_a_killed_record_is_not_revived_by_the_start_of_run_write(tmp_path) -> None:
    """A body that starts after the sweep killed its task must not overwrite the cause of death."""
    from ouroboros.storage.task_results import STATUS_RUNNING, write_task_result

    write_task_result(
        tmp_path, "killed", STATUS_FAILED, scope=TEST_SCOPE, result="Task killed by hard timeout after 1800s."
    )

    write_task_result(tmp_path, "killed", STATUS_RUNNING, scope=TEST_SCOPE, result="Task is running.")

    loaded = load_task_result(tmp_path, "killed")
    assert loaded is not None
    assert loaded["status"] == STATUS_FAILED
    assert loaded["result"] == "Task killed by hard timeout after 1800s."


def test_write_failure_result_none_task_id(tmp_path) -> None:
    from tests.helpers.supervisor import bootstrap_agent_runtime_for_tests

    executor = bootstrap_agent_runtime_for_tests(tmp_path).runtime._executor
    executor._write_failure_result(None)
    executor._write_failure_result("")

    store = runtime_persistence.initialize()
    assert store.tasks_repo.list_tasks() == []


def test_drain_all_pending_returns_and_clears_store(tmp_path) -> None:
    from supervisor.queue import TaskQueue

    store = runtime_persistence.initialize()
    task_queue = TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=10,
        hard_timeout=20,
        audit_event_repository=sql_audit_event_repository(),
    )
    task_queue.enqueue_dict({"id": "t1", "type": "task", "scope": TEST_SCOPE.to_dict()})
    task_queue.enqueue_dict({"id": "t2", "type": "evolution", "scope": TEST_SCOPE.to_dict()})
    drained = [dict(record.input) for record in task_queue.drain_all_pending()]

    assert len(drained) == 2
    assert task_queue.list_pending_records() == []


def test_the_sweep_wakes_a_parked_run_whose_question_is_no_longer_open(tmp_path) -> None:
    """A run that died between parking and asking leaves a record nothing else would ever move."""
    from ouroboros.utils import utc_now_iso
    from supervisor.queue import TaskQueue

    store = runtime_persistence.initialize()
    task_queue = TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=10,
        hard_timeout=20,
        audit_event_repository=sql_audit_event_repository(),
    )
    claim_task(store, "parked")
    opened = park_run(store, task_id="parked", confirmation_id="c-1")

    assert task_queue.reconcile_parked_tasks(sql_tool_confirmation_reader()) == [], (
        "a question still open is one the owner may still answer"
    )

    sql_tool_confirmation_reader(store.engine).expire_tool_confirmation(opened.confirmation_id, now_iso=utc_now_iso())

    assert task_queue.reconcile_parked_tasks(sql_tool_confirmation_reader()) == ["parked"]
    assert store.tasks_repo.get_task("parked").status == "queued"


def test_the_sweep_records_how_the_question_it_closed_ended(tmp_path, caplog) -> None:
    """The run that opened it never reaches its own withdrawal, so nothing else records the outcome."""
    from supervisor.queue import TaskQueue

    store = runtime_persistence.initialize()
    task_queue = TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=10,
        hard_timeout=20,
        audit_event_repository=sql_audit_event_repository(),
    )
    claim_task(store, "parked")
    park_run(store, task_id="parked", confirmation_id="c-1")
    store.tasks_repo.request_task_stop("parked", scope=TEST_SCOPE)

    with caplog.at_level(logging.INFO, logger="supervisor.queue"):
        task_queue.reconcile_parked_tasks(sql_tool_confirmation_reader())

    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_status("c-1") == "expired"
    recorded = sql_audit_event_repository(store.engine).list_audit_events(
        event_type="policy.tool_confirmation_expired", limit=10
    )
    assert [event.metadata["confirmation_id"] for event in recorded] == ["c-1"]
    assert "event=tool_confirmation_expired task_id=parked tool=data_write confirmation_id=c-1" in caplog.messages


def test_the_sweep_drops_the_stored_run_state_of_a_task_that_ended(tmp_path) -> None:
    """The transcript a parked round stored has no reader once the task is over."""
    from supervisor.queue import TaskQueue

    store = runtime_persistence.initialize()
    task_queue = TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=10,
        hard_timeout=20,
        audit_event_repository=sql_audit_event_repository(),
    )
    claim_task(store, "parked")
    park_run(store, task_id="parked", confirmation_id="c-1")
    store.tasks_repo.cancel_task("parked")

    task_queue.reconcile_parked_tasks(sql_tool_confirmation_reader())

    assert sql_tool_confirmation_reader(store.engine).take_parked_snapshot("parked") is None


def test_a_departing_runtime_releases_only_its_own_runs(tmp_path) -> None:
    """A departure hands its own work back and leaves everything else exactly where it is."""
    from supervisor.queue import RELEASE_ERROR_CODE
    from tests.helpers.supervisor import bootstrap_agent_runtime_for_tests

    parts = bootstrap_agent_runtime_for_tests(tmp_path)
    runtime = parts.runtime
    store = runtime_persistence.initialize()

    store.tasks_repo.create_task(TaskRequest(id="mine", type="task", text="run", scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(worker_id=0, runtime_id=runtime.runtime_id)
    assert claimed is not None and claimed.task_id == "mine"
    # Another owner's: one owner has one running root run, so a second record of
    # ``TEST_SCOPE``'s would not be claimable while "mine" runs.
    other = scope_with(TEST_SCOPE, owner_id="someone-else")
    store.tasks_repo.create_task(TaskRequest(id="theirs", type="task", text="run", scope=other.to_dict()))
    elsewhere = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="another-instance")
    assert elsewhere is not None and elsewhere.task_id == "theirs"
    store.tasks_repo.create_task(
        TaskRequest(id="pend1", type="evolution", text="pend", scope=TEST_SCOPE.to_dict()), status="queued"
    )
    store.tasks_repo.create_task(
        TaskRequest(id="pend2", type="task", text="pend", scope=TEST_SCOPE.to_dict()), status="queued"
    )

    runtime.drain(deadline_sec=0.5)

    released = store.tasks_repo.get_task("mine")
    assert released.status not in ("running", STATUS_FAILED)
    assert released.error_code == RELEASE_ERROR_CODE
    # Another instance's run and the queue are none of a departing instance's business.
    assert store.tasks_repo.get_task("theirs").status == "running"
    assert {r.task_id for r in store.tasks_repo.list_tasks(statuses=["queued"])} >= {"pend1", "pend2"}
    clone = [
        r for r in store.tasks_repo.list_tasks(statuses=["queued"]) if r.input.get("instance_release_from") == "mine"
    ]
    assert len(clone) == 1 and clone[0].attempt == 1
