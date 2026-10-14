from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from ouroboros.storage.chat_attachments import file_transaction
from ouroboros.storage.runtime_models import ConversationMessage
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.files import file_client
from tests.helpers.runtime_store import claim_task, park_run
from tests.helpers.shared import TEST_SCOPE
from transport.browser.identity import browser_identity
from transport.browser.infra.repositories.owner_history.repo import SqlOwnerHistoryRepository


def test_history_uses_the_injected_reader(monkeypatch, tmp_path):
    store = runtime_persistence.current
    message = sql_conversation_repository().append_message(
        ConversationMessage(
            **TEST_SCOPE.to_dict(), message_id="message", dedup_key="message", content="question", actor_type="agent"
        ),
        scope=TEST_SCOPE,
    )
    claim_task(store)
    park_run(store)
    reader = Mock(wraps=SqlOwnerHistoryRepository(store.engine, history_transactions=file_transaction))
    reader.load_history = AsyncMock(
        wraps=SqlOwnerHistoryRepository(store.engine, history_transactions=file_transaction).load_history
    )
    resolver = SimpleNamespace(scope_for=AsyncMock(return_value=TEST_SCOPE))
    monkeypatch.setattr(browser_identity, "_resolver", resolver)
    with file_client(tmp_path) as client:
        client.app.state.owner_history_repository = reader
        response = client.get("/api/chat/history?limit=5")

    assert response.status_code == 200
    assert [row["message_id"] for row in response.json()["messages"]] == [message.message_id]
    reader.load_history.assert_awaited_once_with(scope=TEST_SCOPE, limit=5, before_ts="")
