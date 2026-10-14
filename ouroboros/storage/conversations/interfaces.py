from abc import ABC, abstractmethod
from contextlib import AbstractContextManager

from new_core.identity import OwnerScope
from ouroboros.storage.runtime_models import ContextSummary, ConversationMessage

ConversationMetadata = dict[str, object]


class IConversationRepository(ABC):
    @property
    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]: ...

    @abstractmethod
    def append_message(self, message: ConversationMessage, *, scope: OwnerScope) -> ConversationMessage | None: ...

    @abstractmethod
    def get_message(self, message_id: str) -> ConversationMessage | None: ...

    @abstractmethod
    def get_message_by_dedup_key(self, dedup_key: str, *, scope: OwnerScope) -> ConversationMessage | None: ...

    @abstractmethod
    def claim_message_handoff(
        self, message_id: str, *, scope: OwnerScope, claim: str
    ) -> ConversationMessage | None: ...

    @abstractmethod
    def message_handoff_remaining(self, message_id: str, *, scope: OwnerScope, claim: str) -> float: ...

    @abstractmethod
    def list_message_handoffs(self, *, after_id: str, limit: int) -> list[ConversationMessage]: ...

    @abstractmethod
    def update_message_metadata(
        self, message_id: str, metadata: ConversationMetadata, *, expected_metadata: ConversationMetadata | None = None
    ) -> bool: ...

    @abstractmethod
    def delete_message(self, message_id: str) -> bool: ...

    @abstractmethod
    def list_messages(
        self,
        *,
        scope: OwnerScope,
        limit: int = 500,
        offset: int = 0,
        actor_type: str | None = None,
        source: str | None = None,
        after_message_id: str | None = None,
        include_progress: bool = True,
        newest_first: bool = False,
    ) -> list[ConversationMessage]: ...

    @abstractmethod
    def list_owner_messages(
        self,
        *,
        scope: OwnerScope,
        limit: int = 10000,
        actor_type: str | None = None,
        source: str | None = None,
        after_message_id: str | None = None,
        include_progress: bool = False,
        newest_first: bool = False,
    ) -> list[ConversationMessage]: ...

    @abstractmethod
    def list_pair_messages(
        self,
        *,
        owner_id: str,
        agent_id: str,
        limit: int = 500,
        actor_type: str | None = None,
        source: str | None = None,
        include_progress: bool = False,
        newest_first: bool = False,
        unprocessed_only: bool = False,
    ) -> list[ConversationMessage]: ...

    @abstractmethod
    def list_owner_agent_pairs(
        self, *, limit: int = 1000, start_after: tuple[str, str] | None = None
    ) -> list[tuple[str, str]]: ...

    @abstractmethod
    def list_chat_history_messages(
        self,
        *,
        scope: OwnerScope,
        limit: int = 100,
        offset: int = 0,
        created_from: str | None = None,
        created_to: str | None = None,
        after_message_id: str | None = None,
        through_message_id: str | None = None,
        oldest_first: bool = False,
        message_ids: list[str] | None = None,
    ) -> list[ConversationMessage]: ...

    @abstractmethod
    def count_messages(
        self,
        *,
        scope: OwnerScope,
        after_message_id: str | None = None,
    ) -> int: ...

    @abstractmethod
    def upsert_context_summary(self, summary: ContextSummary) -> ContextSummary: ...

    @abstractmethod
    def get_latest_context_summary(self, scope: OwnerScope) -> ContextSummary | None: ...
