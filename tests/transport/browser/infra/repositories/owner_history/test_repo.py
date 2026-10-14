import time

import pytest

from infra.sql_connection import store_deadline
from ouroboros.storage.chat_attachments import file_transaction
from ouroboros.storage.runtime_models import ConversationMessage
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.runtime_store import claim_task, park_run
from tests.helpers.shared import TEST_SCOPE, scope_with
from transport.browser.infra.repositories.owner_history.repo import SqlOwnerHistoryRepository


@parametrize_from_dict(
    {
        "owner": {"scope": scope_with(TEST_SCOPE, owner_id="other"), "visible": False},
        "agent": {"scope": scope_with(TEST_SCOPE, agent_id="other"), "visible": False},
        "chat": {"scope": scope_with(TEST_SCOPE, chat_id="other"), "visible": False},
        "surface": {"scope": scope_with(TEST_SCOPE, surface_id="dialogs"), "visible": True},
    }
)
def test_history_filters_owner_agent_chat_and_keeps_chronological_window(scope, visible):
    store = runtime_persistence.current
    for ident, timestamp in [("b", "2"), ("a", "1"), ("c", "2")]:
        sql_conversation_repository(store.engine).append_message(
            ConversationMessage(
                message_id=ident,
                dedup_key=ident,
                owner_id=TEST_SCOPE.owner_id,
                agent_id=TEST_SCOPE.agent_id,
                chat_id=TEST_SCOPE.chat_id,
                content=ident,
                created_at=timestamp,
            ),
            scope=TEST_SCOPE,
        )
    rows, confirmation = SqlOwnerHistoryRepository(
        store.engine, history_transactions=file_transaction
    ).list_owner_messages_with_open_confirmation(scope=scope, limit=2)
    assert [row.message_id for row in rows] == (["b", "c"] if visible else [])
    assert confirmation is None


def test_confirmation_uses_the_full_scope_and_needs_a_message_row():
    store = runtime_persistence.current
    claim_task(store)
    opened = park_run(store)
    repository = SqlOwnerHistoryRepository(store.engine, history_transactions=file_transaction)
    assert repository.list_owner_messages_with_open_confirmation(scope=TEST_SCOPE, limit=5) == ([], None)
    sql_conversation_repository(store.engine).append_message(
        ConversationMessage(
            message_id="message",
            dedup_key="message",
            owner_id=TEST_SCOPE.owner_id,
            agent_id=TEST_SCOPE.agent_id,
            chat_id=TEST_SCOPE.chat_id,
            content="question",
            created_at="2026",
        ),
        scope=TEST_SCOPE,
    )
    rows, confirmation = repository.list_owner_messages_with_open_confirmation(scope=TEST_SCOPE, limit=5)
    assert [row.content for row in rows] == ["question"]
    assert confirmation == opened.confirmation_id
    other_rows, other_confirmation = repository.list_owner_messages_with_open_confirmation(
        scope=scope_with(TEST_SCOPE, surface_id="dialogs"), limit=5
    )
    assert other_rows == rows and other_confirmation is None


def test_history_read_respects_deadline():
    repository = SqlOwnerHistoryRepository(runtime_persistence.current.engine, history_transactions=file_transaction)
    token = store_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            repository.list_owner_messages_with_open_confirmation(scope=TEST_SCOPE, limit=5)
    finally:
        store_deadline.reset(token)
