import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from ouroboros.storage.runtime_models import TaskEpisode, TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.conversation_conformance import message
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.sql_faults import fail_sql


def test_context_capture_uses_the_admission_connection_including_scratchpad():
    tasks = runtime_persistence.current.tasks_repo
    conversations_repo = runtime_persistence.current.conversations_repo
    transaction_manager = runtime_persistence.current.transaction_manager
    engine = runtime_persistence.current.engine
    connections = set()

    def observe(conn, cursor, statement, parameters, context, executemany):
        connections.add(id(conn))

    with tasks.transaction, transaction_manager.connection() as conn:
        conversations_repo.append_message(message("submission", actor_type="owner"), scope=TEST_SCOPE)
        conn.execute(
            text(
                "INSERT INTO scratchpad_blocks (owner_id, agent_id, surface_id, chat_id, job_id, ts, source, content, status) "
                "VALUES (:owner_id, :agent_id, :surface_id, :chat_id, '', '', '', 'uncommitted scratchpad', 'active')"
            ),
            TEST_SCOPE.to_dict(),
        )
        event.listen(engine, "before_cursor_execute", observe)
        try:
            admitted = runtime_persistence.current.create_root_task.execute(
                TaskRequest(id="job", detached=True, through_message_id="submission", scope=TEST_SCOPE.to_dict()),
                enabled=True,
                max_active=3,
            )
        finally:
            event.remove(engine, "before_cursor_execute", observe)
        assert connections == {id(conn)}
        assert "uncommitted scratchpad" in admitted.input["inherited_context_seed"]
        assert admitted.input["history_message_ids"] == ["submission"]


def test_failed_admission_preserves_outer_transaction_and_can_be_retried():
    tasks = runtime_persistence.current.tasks_repo
    conversations_repo = runtime_persistence.current.conversations_repo
    with tasks.transaction:
        conversations_repo.append_message(message("submission", actor_type="owner"), scope=TEST_SCOPE)
        request = TaskRequest(id="job", detached=True, through_message_id="submission", scope=TEST_SCOPE.to_dict())
        with fail_sql("INSERT INTO tasks", OSError("insert failed")), pytest.raises(OSError, match="insert failed"):
            runtime_persistence.current.create_root_task.execute(request, enabled=True, max_active=3)
        assert tasks.get_task("job") is None
        assert conversations_repo.get_message("submission") is not None
        admitted = runtime_persistence.current.create_root_task.execute(request, enabled=True, max_active=3)
    assert tasks.get_task(admitted.task_id).input["history_message_ids"] == ["submission"]


def test_admission_does_not_overwrite_a_task_id_from_another_job():
    tasks = runtime_persistence.current.tasks_repo
    tasks.create_task(TaskRequest(id="collision", job_id="original", text="keep", scope=TEST_SCOPE.to_dict()))
    with pytest.raises(IntegrityError):
        runtime_persistence.current.create_root_task.execute(
            TaskRequest(id="collision", job_id="different", text="replace", scope=TEST_SCOPE.to_dict()),
            enabled=True,
            max_active=3,
        )
    stored = tasks.get_task("collision")
    assert stored.job_id == "original" and stored.input["text"] == "keep"


def test_episode_capture_query_count_does_not_grow_with_episode_count():
    persistence = runtime_persistence.current
    persistence.conversations_repo.append_message(message("submission", actor_type="owner"), scope=TEST_SCOPE)
    counts = []
    statements = []
    expected_snapshot = {}

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    for start, stop in ((0, 1), (1, 30)):
        for index in range(start, stop):
            episode = persistence.memory_repo.record_task_episode(
                TaskEpisode(**TEST_SCOPE.to_dict(), episode_id=f"episode-{index}", task_id=f"expired-task-{index}")
            )
            expected_snapshot[episode.episode_id] = episode.snapshot_version()
        statements.clear()
        event.listen(persistence.engine, "before_cursor_execute", observe)
        try:
            admitted = persistence.create_root_task.execute(
                TaskRequest(
                    id=f"admitted-{stop}", detached=True, through_message_id="submission", scope=TEST_SCOPE.to_dict()
                ),
                enabled=True,
                max_active=3,
            )
        finally:
            event.remove(persistence.engine, "before_cursor_execute", observe)
        counts.append(len(statements))
        stored = persistence.tasks_repo.get_task(admitted.task_id)
        assert stored.input["episode_snapshot"] == expected_snapshot
    assert counts[1] == counts[0]


def test_episode_capture_keeps_only_completed_or_missing_tasks():
    persistence = runtime_persistence.current
    expected_snapshot = {}
    with persistence.tasks_repo.transaction:
        persistence.conversations_repo.append_message(message("submission", actor_type="owner"), scope=TEST_SCOPE)
        for status in ("completed", "failed", "queued"):
            persistence.tasks_repo.create_task(TaskRequest(id=status, scope=TEST_SCOPE.to_dict()), status=status)
        for index, task_id in enumerate(("completed", "failed", "queued", "missing", "", "completed")):
            episode = persistence.memory_repo.record_task_episode(
                TaskEpisode(**TEST_SCOPE.to_dict(), episode_id=f"episode-{index}", task_id=task_id)
            )
            if task_id in ("completed", "missing"):
                expected_snapshot[episode.episode_id] = episode.snapshot_version()
        admitted = persistence.create_root_task.execute(
            TaskRequest(id="admitted", detached=True, through_message_id="submission", scope=TEST_SCOPE.to_dict()),
            enabled=True,
            max_active=3,
        )
    assert persistence.tasks_repo.get_task(admitted.task_id).input["episode_snapshot"] == expected_snapshot
