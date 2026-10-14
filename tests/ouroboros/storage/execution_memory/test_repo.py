import time

import pytest
from sqlalchemy import event, text

from infra.sql_connection import store_deadline
from ouroboros.storage.execution_memory.repository import SqlExecutionMemoryRepository
from ouroboros.storage.runtime_models import ToolInvocation
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.tool_invocation_conformance import ToolInvocationRepositoryConformance


class TestSqlExecutionMemoryRepository(ToolInvocationRepositoryConformance):
    def make_repository(self) -> SqlExecutionMemoryRepository:
        return sql_execution_memory_repository()


@parametrize_from_dict({"read": {"write": False}, "write": {"write": True}})
def test_operations_respect_an_expired_deadline(write):
    repository = sql_execution_memory_repository()
    token = store_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            if write:
                repository.record_tool_invocation(ToolInvocation(**TEST_SCOPE.to_dict(), task_id="task"))
            else:
                repository.list_tool_invocations(task_id="task")
    finally:
        store_deadline.reset(token)
    assert repository.list_tool_invocations(task_id="task") == []


@parametrize_from_dict({"insert": {"existing": False}, "conflict update": {"existing": True}})
def test_deadline_expiring_before_commit_rolls_back_the_write(existing):
    engine = runtime_persistence.current.engine
    repository = sql_execution_memory_repository(engine)
    invocation = ToolInvocation(**TEST_SCOPE.to_dict(), tool_invocation_id="call", task_id="task", status="old")
    if existing:
        repository.record_tool_invocation(invocation)
    before = repository.list_tool_invocations(task_id="task")
    invocation.status = "new"

    def expire_after_write(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO tool_invocations"):
            conn.info["operation_deadline"] = time.monotonic() - 1

    token = store_deadline.set(time.monotonic() + 60)
    event.listen(engine, "after_cursor_execute", expire_after_write)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            repository.record_tool_invocation(invocation)
    finally:
        event.remove(engine, "after_cursor_execute", expire_after_write)
        store_deadline.reset(token)
    assert repository.list_tool_invocations(task_id="task") == before


@parametrize_from_dict(
    {
        "broken JSON": {"body": "broken"},
        "non-object": {"body": "[]"},
        "legacy axes": {
            "body": '{"owner_id":"wrong","agent_id":"wrong","chat_id":"wrong","conversation_id":"retired"}'
        },
    }
)
def test_column_scope_wins_over_legacy_or_invalid_payload(body):
    repository = sql_execution_memory_repository()
    original = ToolInvocation(**TEST_SCOPE.to_dict(), tool_invocation_id="call", task_id="task")
    repository.record_tool_invocation(original)
    with runtime_persistence.current.engine.begin() as conn:
        conn.execute(text("UPDATE tool_invocations SET payload_json=:body, search_terms_json='{}'"), {"body": body})
    assert repository.list_tool_invocations(task_id="task") == [original]


def test_invalid_legacy_row_is_skipped_without_filling_the_limit(caplog):
    repository = sql_execution_memory_repository()
    for ident in ["a", "b"]:
        repository.record_tool_invocation(
            ToolInvocation(**TEST_SCOPE.to_dict(), tool_invocation_id=ident, task_id="task", created_at="same")
        )
    with runtime_persistence.current.engine.begin() as conn:
        conn.execute(text("UPDATE tool_invocations SET chat_id='' WHERE tool_invocation_id='b'"))
    assert repository.list_tool_invocations(task_id="task", limit=1) == []
    assert [item.tool_invocation_id for item in repository.list_tool_invocations(task_id="task")] == ["a"]
    assert "Skipped a tool_invocations row" in caplog.text
