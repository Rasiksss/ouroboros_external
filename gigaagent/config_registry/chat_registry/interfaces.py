from __future__ import annotations

from abc import ABC, abstractmethod

from config_registry.chat_registry.domain import ChatEntity
from new_core.identity import OwnerScope


class ChatRegistryStore(ABC):
    @abstractmethod
    def ensure_chat(self, scope: OwnerScope, *, provisional_title: str) -> ChatEntity:
        pass

    @abstractmethod
    def get_chat(self, scope: OwnerScope) -> ChatEntity | None:
        pass

    @abstractmethod
    def claim_title_attempt(self, scope: OwnerScope, *, max_attempts: int) -> bool:
        pass

    @abstractmethod
    def store_generated_title(self, scope: OwnerScope, *, title: str) -> bool:
        pass

    @abstractmethod
    def delete_owner_chats(self, *, owner_id: str, agent_id: str) -> int:
        pass


class IChatRegistryRepo(ABC):
    @abstractmethod
    async def list_chats(self, owner_id: str, agent_id: str, *, include_retired: bool = False) -> list[ChatEntity]:
        pass

    @abstractmethod
    async def get_chat(self, owner_id: str, agent_id: str, chat_id: str) -> ChatEntity | None:
        pass

    @abstractmethod
    async def claim_title_attempt(self, owner_id: str, agent_id: str, chat_id: str, *, max_attempts: int) -> bool:
        pass

    @abstractmethod
    async def store_generated_title(self, owner_id: str, agent_id: str, chat_id: str, *, title: str) -> bool:
        pass

    @abstractmethod
    async def rename_chat(self, owner_id: str, agent_id: str, chat_id: str, *, title: str) -> None:
        pass

    @abstractmethod
    async def set_chat_active(self, owner_id: str, agent_id: str, chat_id: str, *, is_active: bool) -> None:
        pass


__all__ = ["ChatRegistryStore", "IChatRegistryRepo"]
