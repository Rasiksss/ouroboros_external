from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import pytest
from sqlalchemy import event

from ouroboros.storage.runtime_models import TaskEpisode, TaskRecord, TaskRequest, TaskStopState
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.tasks.repository import SqlTaskRepository
from tests.helpers.conversation_conformance import message
from tests.helpers.conversations import make_sql_runtime_persistence
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE


def test_task_repository_owns_stop_reads_and_writes():
    tasks = runtime_persistence.current.tasks_repo
    assert isinstance(tasks, SqlTaskRepository)
    tasks.create_task(TaskRequest(id="stop", scope=TEST_SCOPE.to_dict()), status="running")
    assert tasks.request_task_stop("stop", scope=TEST_SCOPE)
    assert tasks.read_task_stop_state("stop") == TaskStopState(stop_requested=True, is_terminal=False)


def test_task_writes_and_reads_join_outer_rollback():
    tasks = runtime_persistence.current.tasks_repo
    with pytest.raises(RuntimeError, match="abort"):
        with tasks.transaction:
            tasks.create_task(TaskRequest(id="rollback", scope=TEST_SCOPE.to_dict()), status="running")
            assert tasks.request_task_stop("rollback", scope=TEST_SCOPE)
            assert tasks.read_task_stop_state("rollback").stop_requested
            raise RuntimeError("abort")
    assert tasks.get_task("rollback") is None


@parametrize_from_dict(
    {
        "latest completed attempt": {"changes": {"status": "completed"}, "expected": "completed"},
        "latest queued attempt": {"changes": {"status": "queued"}, "expected": "queued"},
        "other owner": {"changes": {"owner_id": "other-owner"}, "expected": "failed"},
        "other agent": {"changes": {"agent_id": "other-agent"}, "expected": "failed"},
        "other chat remains owner scoped": {"changes": {"chat_id": "other-chat"}, "expected": "completed"},
        "child is not a root job": {"changes": {"parent_task_id": "parent"}, "expected": "failed"},
        "non task is not a root job": {"changes": {"task_type": "message"}, "expected": "failed"},
    }
)
def test_batched_statuses_preserve_current_root_attempt_precedence(changes, expected):
    tasks = runtime_persistence.current.tasks_repo
    original = TaskRecord(**TEST_SCOPE.to_dict(), task_id="job", job_id="job", status="failed")
    latest = original.model_copy(update={"task_id": "retry", "lineage_ordinal": 1, "status": "completed", **changes})
    with tasks.transaction:
        tasks.insert_task(original)
        tasks.insert_task(latest)
        assert tasks.get_job_or_task_statuses(["job", "job", "missing"], scope=TEST_SCOPE) == {"job": expected}


def test_batched_statuses_fall_back_to_task_ids_and_omit_missing_rows():
    tasks = runtime_persistence.current.tasks_repo
    child = TaskRecord(
        **TEST_SCOPE.to_dict(), task_id="child", job_id="child-job", parent_task_id="parent", status="completed"
    )
    legacy = TaskRecord(**TEST_SCOPE.to_dict(), task_id="legacy", status="failed")
    legacy.job_id = ""
    with tasks.transaction:
        tasks.insert_task(child)
        tasks.insert_task(legacy)
        assert tasks.get_job_or_task_statuses(["child", "legacy", "missing"], scope=TEST_SCOPE) == {
            "child": "completed",
            "legacy": "failed",
        }


def test_empty_status_batch_does_not_query_the_database():
    engine = runtime_persistence.current.engine
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        assert runtime_persistence.current.tasks_repo.get_job_or_task_statuses([], scope=TEST_SCOPE) == {}
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert statements == []


def test_nested_rollback_keeps_outer_task():
    tasks = runtime_persistence.current.tasks_repo
    with tasks.transaction:
        tasks.create_task(TaskRequest(id="outer", scope=TEST_SCOPE.to_dict()))
        with pytest.raises(ValueError, match="nested"):
            with tasks.transaction:
                tasks.create_task(TaskRequest(id="inner", scope=TEST_SCOPE.to_dict()))
                raise ValueError("nested")
        assert tasks.get_task("outer") is not None
        assert tasks.get_task("inner") is None
    assert tasks.get_task("outer") is not None


def test_declined_claim_rolls_back_only_its_operation():
    tasks = runtime_persistence.current.tasks_repo

    @contextmanager
    def decline(_task):
        yield False

    with tasks.transaction:
        tasks.create_task(TaskRequest(id="queued", scope=TEST_SCOPE.to_dict()))
        assert tasks.claim_next_task(runtime_id="runtime", claim_guard=decline) is None
        assert tasks.get_task("queued").status == "queued"
        tasks.create_task(TaskRequest(id="after", scope=TEST_SCOPE.to_dict()))
    assert tasks.get_task("queued").status == "queued"
    assert tasks.get_task("after") is not None


def test_admission_and_context_repositories_roll_back_together():
    tasks = runtime_persistence.current.tasks_repo
    conversations_repo = runtime_persistence.current.conversations_repo
    memory = runtime_persistence.current.memory_repo
    observer = make_sql_runtime_persistence(runtime_persistence.current.engine, db_url="").tasks_repo
    with pytest.raises(ValueError, match="abort"):
        with tasks.transaction:
            conversations_repo.append_message(message("submission", actor_type="owner"), scope=TEST_SCOPE)
            episode = memory.record_task_episode(
                TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="episode", task_id="old")
            )
            admitted = runtime_persistence.current.create_root_task.execute(
                TaskRequest(id="admitted", detached=True, scope=TEST_SCOPE.to_dict(), through_message_id="submission"),
                enabled=True,
                max_active=3,
            )
            assert admitted.input["history_message_ids"] == ["submission"]
            assert admitted.input["episode_snapshot"] == {episode.episode_id: episode.snapshot_version()}
            assert observer.get_task("admitted") is None
            raise ValueError("abort")
    assert tasks.get_task("admitted") is None
    assert conversations_repo.get_message("submission") is None
    assert memory.list_task_episodes(scope=TEST_SCOPE) == []


def test_parallel_transactions_on_one_repository_are_isolated():
    tasks = runtime_persistence.current.tasks_repo
    barrier = Barrier(2)

    def write(task_id):
        try:
            with tasks.transaction:
                tasks.create_task(TaskRequest(id=task_id, scope=TEST_SCOPE.to_dict()))
                barrier.wait(timeout=5)
                if task_id == "rollback":
                    raise ValueError("abort")
        except ValueError:
            pass

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, ["commit", "rollback"]))
    assert tasks.get_task("commit") is not None
    assert tasks.get_task("rollback") is None


def test_claim_is_committed_before_guard_exits():
    tasks = runtime_persistence.current.tasks_repo
    observer = make_sql_runtime_persistence(runtime_persistence.current.engine, db_url="").tasks_repo
    tasks.create_task(TaskRequest(id="guarded", scope=TEST_SCOPE.to_dict()))
    observed = []

    @contextmanager
    def guard(task):
        yield True
        observed.append(observer.get_task(task.task_id).status)

    assert tasks.claim_next_task(runtime_id="runtime", claim_guard=guard) is not None
    assert observed == ["running"]


@parametrize_from_dict(
    {
        "commit applied": {"committed": True, "successor_id": "successor"},
        "commit failed": {"committed": False, "successor_id": "successor"},
        "commit applied with generated id": {"committed": True, "successor_id": ""},
        "commit failed with generated id": {"committed": False, "successor_id": ""},
    }
)
def test_retry_recovers_only_a_persisted_commit(monkeypatch, committed, successor_id):
    tasks = runtime_persistence.current.tasks_repo
    tasks.create_task(TaskRequest(id="original", scope=TEST_SCOPE.to_dict()))
    tasks.claim_next_task(runtime_id="runtime")
    original_commit = runtime_persistence.current.engine.dialect.do_commit

    def uncertain_commit(connection):
        if committed:
            original_commit(connection)
        raise OSError("commit acknowledgement lost")

    successor = TaskRequest(id=successor_id, scope=TEST_SCOPE.to_dict())
    with monkeypatch.context() as patch:
        patch.setattr(runtime_persistence.current.engine.dialect, "do_commit", uncertain_commit)
        if committed:
            result = tasks.transition_running(
                "original", kind="lease_lost", result="retry", attempt=2, successor=successor, cutoff_iso="9999"
            )
            assert result is not None and result[1]
        else:
            with pytest.raises(OSError, match="acknowledgement lost"):
                tasks.transition_running(
                    "original", kind="lease_lost", result="retry", attempt=2, successor=successor, cutoff_iso="9999"
                )
    assert tasks.get_task("original").status == ("failed" if committed else "running")
    assert len(tasks.list_tasks()) == (2 if committed else 1)
    assert successor.id == successor_id


def test_standalone_attempt_check_does_not_read_or_restore_timeout():
    statements = []
    engine = runtime_persistence.current.engine

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert not runtime_persistence.current.tasks_repo.is_task_attempt_active("missing")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 2
    assert statements[0] == "SELECT clock_timestamp()"
    assert statements[1].startswith("SELECT 1 FROM tasks")


def test_borrowed_attempt_check_restores_timeout():
    transaction_manager = runtime_persistence.current.transaction_manager
    with transaction_manager.transaction, transaction_manager.connection() as conn:
        conn.exec_driver_sql("SET LOCAL statement_timeout='20s'")
        assert not runtime_persistence.current.tasks_repo.is_task_attempt_active("missing")
        assert conn.exec_driver_sql("SHOW statement_timeout").scalar_one() == "20s"
