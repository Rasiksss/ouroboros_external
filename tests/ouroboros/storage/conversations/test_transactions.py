import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event

from infra.sql_connection import store_deadline
from infra.sql_transaction import SqlTransactionManager
from ouroboros.storage.conversations.repository import SqlConversationRepository
from ouroboros.storage.runtime_models import ContextSummary, TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.conversation_conformance import message
from tests.helpers.shared import TEST_SCOPE


def test_admission_history_and_summary_use_the_task_connection():
    store = runtime_persistence.current
    repo = runtime_persistence.current.conversations_repo
    repo.append_message(message("submission", actor_type="owner"), scope=TEST_SCOPE)
    repo.upsert_context_summary(
        ContextSummary(
            owner_id=TEST_SCOPE.owner_id,
            agent_id=TEST_SCOPE.agent_id,
            chat_id=TEST_SCOPE.chat_id,
            source_offsets_ref="submission",
            summary_ref="summary",
        )
    )
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if any(table in statement for table in ("messages", "context_summaries", "tasks")):
            statements.append((conn, statement))

    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        task = runtime_persistence.current.create_root_task.execute(
            TaskRequest(id="task", scope=TEST_SCOPE.to_dict(), through_message_id="submission"),
            enabled=True,
            max_active=3,
        )
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)
    assert task.input["history_message_ids"] == ["submission"]
    assert task.input["committed_summary_snapshot"] == "summary"
    assert len({id(conn) for conn, _ in statements}) == 1
    assert any("context_summaries" in statement for _, statement in statements)


def test_uncommitted_conversations_join_caller_connection_and_rollback():
    engine = runtime_persistence.current.engine
    transaction_manager = SqlTransactionManager(engine)
    repo = SqlConversationRepository(transaction_manager)
    with pytest.raises(ValueError, match="abort"), transaction_manager.transaction:
        repo.append_message(message("uncommitted"), scope=TEST_SCOPE)
        assert repo.get_message("uncommitted") is not None
        assert SqlConversationRepository(SqlTransactionManager(engine)).get_message("uncommitted") is None
        raise ValueError("abort")
    assert repo.get_message("uncommitted") is None


def test_concurrent_handoff_has_one_winner():
    engine = runtime_persistence.current.engine
    repo = SqlConversationRepository(SqlTransactionManager(engine))
    repo.append_message(message("delivery", actor_type="agent"), scope=TEST_SCOPE)
    barrier = Barrier(2)

    def claim(token):
        barrier.wait(timeout=5)
        return repo.claim_message_handoff("delivery", scope=TEST_SCOPE, claim=token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["a", "b"]))
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result is not None)
    assert repo.get_message("delivery").metadata["handoff_claim"] == winner.metadata["handoff_claim"]


def test_expired_deadline_refuses_write():
    repo = runtime_persistence.current.conversations_repo
    token = store_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError):
            repo.append_message(message("late"), scope=TEST_SCOPE)
    finally:
        store_deadline.reset(token)
    assert repo.get_message("late") is None
