from abc import ABC, abstractmethod

import pytest

from ouroboros.storage.conversations.interfaces import IConversationRepository
from ouroboros.storage.runtime_backend import MessageAnchorMissingError
from ouroboros.storage.runtime_models import ContextSummary, ConversationMessage
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with


def message(message_id: str, *, scope=TEST_SCOPE, **fields) -> ConversationMessage:
    return ConversationMessage(
        owner_id=scope.owner_id,
        agent_id=scope.agent_id,
        chat_id=scope.chat_id,
        message_id=message_id,
        dedup_key=message_id,
        created_at="2026-09-23T10:00:00Z",
        content=message_id,
        **fields,
    )


class ConversationConformance(ABC):
    @abstractmethod
    def repository(self) -> IConversationRepository: ...

    def test_duplicate_does_not_replace_existing_message(self):
        repo = self.repository()
        repo.append_message(message("a"), scope=TEST_SCOPE)
        duplicate = message("b").model_copy(update={"dedup_key": "a"})
        assert repo.append_message(duplicate, scope=TEST_SCOPE) is None
        assert repo.get_message("a").content == "a"
        assert repo.get_message("b") is None

    @parametrize_from_dict(
        {
            "owner": {"axes": {"owner_id": "other"}},
            "agent": {"axes": {"agent_id": "other"}},
            "chat": {"axes": {"chat_id": "other"}},
        }
    )
    def test_history_and_summary_are_scoped(self, axes):
        repo = self.repository()
        other = scope_with(TEST_SCOPE, **axes)
        repo.append_message(message("a"), scope=TEST_SCOPE)
        repo.append_message(message("b", scope=other), scope=other)
        repo.upsert_context_summary(
            ContextSummary(
                owner_id=other.owner_id, agent_id=other.agent_id, chat_id=other.chat_id, summary_ref="private"
            )
        )
        assert [row.message_id for row in repo.list_chat_history_messages(scope=TEST_SCOPE)] == ["a"]
        assert repo.get_message_by_dedup_key("b", scope=TEST_SCOPE) is None
        assert repo.get_latest_context_summary(TEST_SCOPE) is None
        with pytest.raises(MessageAnchorMissingError):
            repo.list_chat_history_messages(scope=TEST_SCOPE, through_message_id="b")

    def test_visible_history_filters_before_limit_and_uses_frozen_ids(self):
        repo = self.repository()
        for name in ["a", "b", "c"]:
            repo.append_message(message(name), scope=TEST_SCOPE)
        repo.append_message(message("d", metadata={"is_progress": True}), scope=TEST_SCOPE)
        assert [row.message_id for row in repo.list_owner_messages(scope=TEST_SCOPE, newest_first=True, limit=2)] == [
            "b",
            "c",
        ]
        assert [
            row.message_id
            for row in repo.list_chat_history_messages(
                scope=TEST_SCOPE, after_message_id="a", through_message_id="c", message_ids=["b"]
            )
        ] == ["b"]
        assert repo.list_chat_history_messages(scope=TEST_SCOPE, message_ids=[]) == []
        with pytest.raises(MessageAnchorMissingError):
            repo.list_chat_history_messages(scope=TEST_SCOPE, after_message_id="c", through_message_id="a")

    def test_metadata_compare_and_swap_updates_journal_visibility(self):
        repo = self.repository()
        repo.append_message(message("a", actor_type="owner", metadata={"inbound_processed": False}), scope=TEST_SCOPE)
        assert (
            len(
                repo.list_pair_messages(
                    owner_id=TEST_SCOPE.owner_id, agent_id=TEST_SCOPE.agent_id, unprocessed_only=True
                )
            )
            == 1
        )
        assert not repo.update_message_metadata("a", {}, expected_metadata={})
        assert repo.update_message_metadata(
            "a", {"inbound_processed": True, "is_progress": True}, expected_metadata={"inbound_processed": False}
        )
        assert (
            repo.list_pair_messages(owner_id=TEST_SCOPE.owner_id, agent_id=TEST_SCOPE.agent_id, unprocessed_only=True)
            == []
        )
        assert repo.list_chat_history_messages(scope=TEST_SCOPE) == []
        assert repo.count_messages(scope=TEST_SCOPE) == 1
        assert repo.delete_message("a")
        assert not repo.delete_message("a")

    def test_handoff_claim_refusal_and_confirmation(self):
        repo = self.repository()
        repo.append_message(message("a", actor_type="agent"), scope=TEST_SCOPE)
        claimed = repo.claim_message_handoff("a", scope=TEST_SCOPE, claim="first")
        assert claimed is not None
        assert repo.claim_message_handoff("a", scope=TEST_SCOPE, claim="second") is None
        assert repo.message_handoff_remaining("a", scope=TEST_SCOPE, claim="second") == 0
        assert repo.message_handoff_remaining("a", scope=TEST_SCOPE, claim="first") > 0
        assert repo.update_message_metadata(
            "a", {**claimed.metadata, "handoff_accepted": True}, expected_metadata=claimed.metadata
        )
        assert repo.message_handoff_remaining("a", scope=TEST_SCOPE, claim="first") == 0
        assert repo.claim_message_handoff("a", scope=TEST_SCOPE, claim="third").metadata["handoff_accepted"]

    def test_nested_rollback_keeps_outer_work(self):
        repo = self.repository()
        with repo.transaction:
            repo.append_message(message("a"), scope=TEST_SCOPE)
            with pytest.raises(ValueError):
                with repo.transaction:
                    repo.append_message(message("b"), scope=TEST_SCOPE)
                    raise ValueError("inner")
            assert repo.get_message("b") is None
            repo.upsert_context_summary(
                ContextSummary(
                    owner_id=TEST_SCOPE.owner_id,
                    agent_id=TEST_SCOPE.agent_id,
                    chat_id=TEST_SCOPE.chat_id,
                    summary_ref="committed",
                )
            )
        assert repo.get_message("a") is not None
        assert repo.get_latest_context_summary(TEST_SCOPE).summary_ref == "committed"

    def test_pair_and_agent_pages(self):
        repo = self.repository()
        other = scope_with(TEST_SCOPE, owner_id="zzz")
        repo.append_message(message("a", actor_type="agent"), scope=TEST_SCOPE)
        repo.append_message(message("b", scope=other, actor_type="agent"), scope=other)
        pair = (TEST_SCOPE.owner_id, TEST_SCOPE.agent_id)
        assert repo.list_owner_agent_pairs(limit=1) == [pair]
        assert repo.list_owner_agent_pairs(start_after=pair) == [(other.owner_id, other.agent_id)]
        assert [row.message_id for row in repo.list_message_handoffs(after_id="a", limit=1)] == ["b"]
