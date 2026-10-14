import pytest

from ouroboros.storage.runtime_models import ConversationMessage
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.shared import TEST_SCOPE, scope_with


def test_message_without_attachments_still_requires_matching_scope():
    store = runtime_persistence.current.conversations_repo
    message = ConversationMessage(message_id="wrong-scope", dedup_key="wrong-scope", **TEST_SCOPE.to_dict())
    with pytest.raises(ValueError, match="scope"):
        store.append_message(message, scope=scope_with(TEST_SCOPE, owner_id="another-owner"))
    assert store.get_message(message.message_id) is None
