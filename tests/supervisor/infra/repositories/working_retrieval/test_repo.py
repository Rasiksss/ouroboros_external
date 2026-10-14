import time

import pytest
from sqlalchemy import event, text

from infra.sql_connection import store_deadline
from ouroboros.storage.core_memory.scratchpad.infra.sql import SqlScratchpad
from ouroboros.storage.runtime_models import TaskEpisode, ToolInvocation
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.infra.repositories.working_retrieval.repo import SqlWorkingRetrievalRepository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import TEST_SCOPE


def _seed_expired():
    store = runtime_persistence.current
    sql_execution_memory_repository(store.engine).record_task_episode(
        TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="episode", expires_at="2025")
    )
    sql_execution_memory_repository().record_tool_invocation(
        ToolInvocation(**TEST_SCOPE.to_dict(), tool_invocation_id="call", task_id="task", expires_at="2025")
    )


def test_expired_deadline_rolls_back_both_retrieval_deletes():
    _seed_expired()
    store = runtime_persistence.current

    def expire_after_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM tool_invocations"):
            conn.info["operation_deadline"] = time.monotonic() - 1

    token = store_deadline.set(time.monotonic() + 60)
    event.listen(store.engine, "after_cursor_execute", expire_after_delete)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            SqlWorkingRetrievalRepository(
                store.engine, scratchpad=SqlScratchpad(store.engine)
            ).cleanup_expired_working_retrieval(now_iso="2026")
    finally:
        event.remove(store.engine, "after_cursor_execute", expire_after_delete)
        store_deadline.reset(token)
    assert len(sql_execution_memory_repository(store.engine).list_task_episodes(scope=TEST_SCOPE)) == 1
    assert len(sql_execution_memory_repository().list_tool_invocations(task_id="task")) == 1


def test_scratchpad_failure_occurs_after_retrieval_commit():
    _seed_expired()
    store = runtime_persistence.current
    with store.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO scratchpad_blocks(owner_id, agent_id, surface_id, chat_id, job_id, ts, source, content, status) VALUES ('global', 'main', 'browser', '1', 'gone', '2025', 'agent', 'keep', 'active')"
            )
        )

    def reject_scratchpad_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM scratchpad_blocks"):
            raise OSError("scratchpad unavailable")

    event.listen(store.engine, "before_cursor_execute", reject_scratchpad_delete)
    try:
        with pytest.raises(OSError, match="scratchpad unavailable"):
            SqlWorkingRetrievalRepository(
                store.engine, scratchpad=SqlScratchpad(store.engine)
            ).cleanup_expired_working_retrieval(now_iso="2026")
    finally:
        event.remove(store.engine, "before_cursor_execute", reject_scratchpad_delete)
    assert sql_execution_memory_repository(store.engine).list_task_episodes(scope=TEST_SCOPE) == []
    assert sql_execution_memory_repository().list_tool_invocations(task_id="task") == []
    with store.engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM scratchpad_blocks")).scalar_one() == 1
