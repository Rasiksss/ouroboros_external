import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event

from infra.sql_connection import store_deadline
from infra.sql_transaction import SqlTransactionManager
from ouroboros.storage.conversations.repository import SqlConversationRepository
from ouroboros.storage.execution_memory.repository import SqlExecutionMemoryRepository
from ouroboros.storage.runtime_models import TaskEpisode, TaskRequest, ToolInvocation
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.conversation_conformance import message
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE


def test_episode_and_invocation_rollback_together():
    repo = sql_execution_memory_repository()
    with pytest.raises(ValueError, match="abort"):
        with repo.transaction:
            repo.record_tool_invocation(ToolInvocation(**TEST_SCOPE.to_dict(), task_id="task"))
            repo.record_task_episode(TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="episode", task_id="task"))
            assert len(repo.list_task_episodes(scope=TEST_SCOPE)) == 1
            raise ValueError("abort")
    assert repo.list_tool_invocations(task_id="task") == []
    assert repo.list_task_episodes(scope=TEST_SCOPE) == []


def test_bound_memory_and_conversations_share_uncommitted_connection():
    engine = runtime_persistence.current.engine
    transaction_manager = SqlTransactionManager(engine)
    repo = SqlExecutionMemoryRepository(transaction_manager)
    conversations_repo = SqlConversationRepository(transaction_manager)
    observer = sql_execution_memory_repository(engine)
    with pytest.raises(ValueError, match="abort"), transaction_manager.transaction:
        conversations_repo.append_message(message("uncommitted"), scope=TEST_SCOPE)
        repo.record_task_episode(TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="uncommitted"))
        repo.record_tool_invocation(ToolInvocation(**TEST_SCOPE.to_dict(), task_id="task"))
        assert len(repo.list_task_episodes(scope=TEST_SCOPE)) == 1
        assert observer.list_task_episodes(scope=TEST_SCOPE) == []
        assert observer.list_tool_invocations(task_id="task") == []
        raise ValueError("abort")
    assert conversations_repo.get_message("uncommitted") is None
    assert repo.list_task_episodes(scope=TEST_SCOPE) == []
    assert repo.list_tool_invocations(task_id="task") == []


def test_admission_captures_episode_versions_on_the_task_connection():
    store = runtime_persistence.current
    repo = runtime_persistence.current.memory_repo
    runtime_persistence.current.conversations_repo.append_message(
        message("submission", actor_type="owner"), scope=TEST_SCOPE
    )
    episode = repo.record_task_episode(TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="episode", task_id="old"))
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if any(table in statement for table in ("messages", "tasks", "task_episodes")):
            statements.append((conn, statement))

    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        task = runtime_persistence.current.create_root_task.execute(
            TaskRequest(id="task", detached=True, scope=TEST_SCOPE.to_dict(), through_message_id="submission"),
            enabled=True,
            max_active=3,
        )
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)
    assert task.input["episode_snapshot"] == {episode.episode_id: episode.snapshot_version()}
    assert len({id(conn) for conn, _ in statements}) == 1
    assert any("FROM task_episodes" in statement for _, statement in statements)


def test_concurrent_logical_episode_replays_return_the_first_committed_row():
    repo = sql_execution_memory_repository()
    barrier = Barrier(2)

    def record(answer):
        barrier.wait(timeout=5)
        return repo.record_task_episode(
            TaskEpisode(
                **TEST_SCOPE.to_dict(), episode_id="logical", answer_preview=answer, payload={"logical_job": True}
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(record, ["a", "b"]))
    assert first == second
    assert repo.list_task_episodes(scope=TEST_SCOPE) == [first]


@parametrize_from_dict({"insert": {"existing": False}, "update": {"existing": True}})
def test_episode_deadline_before_commit_restores_previous_row(existing):
    engine = runtime_persistence.current.engine
    repo = sql_execution_memory_repository(engine)
    episode = TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="episode", answer_preview="before")
    if existing:
        repo.record_task_episode(episode)
    before = repo.list_task_episodes(scope=TEST_SCOPE)

    def expire(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT * FROM task_episodes WHERE episode_id="):
            conn.info["operation_deadline"] = time.monotonic() - 1

    token = store_deadline.set(time.monotonic() + 60)
    event.listen(engine, "after_cursor_execute", expire)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            repo.record_task_episode(episode.model_copy(update={"answer_preview": "after"}))
    finally:
        event.remove(engine, "after_cursor_execute", expire)
        store_deadline.reset(token)
    assert repo.list_task_episodes(scope=TEST_SCOPE) == before
