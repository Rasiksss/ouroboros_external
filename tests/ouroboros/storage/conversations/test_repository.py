import pytest

from infra.sql_transaction import SqlTransactionManager
from ouroboros.storage.conversations.repository import SqlConversationRepository
from ouroboros.storage.runtime_models import ContextSummary, ConversationMessage
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.conversation_conformance import ConversationConformance
from tests.helpers.shared import TEST_SCOPE


def test_messages_and_summary_rollback_together():
    repository = SqlConversationRepository(SqlTransactionManager(runtime_persistence.current.engine))
    with pytest.raises(ValueError):
        with repository.transaction:
            repository.append_message(
                ConversationMessage(**TEST_SCOPE.to_dict(), message_id="rolled-back", content="hi"), scope=TEST_SCOPE
            )
            repository.upsert_context_summary(ContextSummary(**TEST_SCOPE.to_dict(), summary_ref="summary"))
            assert repository.get_message("rolled-back") is not None
            raise ValueError("abort")
    assert repository.get_message("rolled-back") is None
    assert repository.get_latest_context_summary(TEST_SCOPE) is None


class TestSqlConversations(ConversationConformance):
    def repository(self):
        return SqlConversationRepository(SqlTransactionManager(runtime_persistence.current.engine))
