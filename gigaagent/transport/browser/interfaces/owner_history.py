from abc import ABC, abstractmethod

from new_core.identity import OwnerScope
from ouroboros.storage.message_history import HistoryWindow
from ouroboros.storage.runtime_models import ConversationMessage


class IOwnerHistoryRepository(ABC):
    @abstractmethod
    def list_owner_messages_with_open_confirmation(
        self, *, scope: OwnerScope, limit: int
    ) -> tuple[list[ConversationMessage], str | None]:
        pass

    @abstractmethod
    async def load_history(self, *, scope: OwnerScope, limit: int, before_ts: str = "") -> HistoryWindow: ...
