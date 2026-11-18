from __future__ import annotations

from abc import ABC, abstractmethod


class UserSettingsStore(ABC):
    @abstractmethod
    def get_setting_value(self, owner_id: str, agent_id: str, key: str) -> str | None:
        pass


class IAsyncUserSettingsStore(ABC):
    """The reader's async face: the settings surface, and every caller on an event loop.

    The synchronous reader is the runtime's, where a scope's key is resolved on a worker
    thread; a surface reporting a whole agent's settings and a loop that may not block read
    through the async engine, mirroring how the cache is one boundary with two faces.
    """

    @abstractmethod
    async def get_setting_values(self, owner_id: str, agent_id: str) -> dict[str, str]:
        pass

    @abstractmethod
    async def get_setting_value(self, owner_id: str, agent_id: str, key: str) -> str | None:
        pass


class IUserSettingsRepo(ABC):
    @abstractmethod
    async def set_override(self, owner_id: str, agent_id: str, key: str, value: str) -> None:
        pass

    @abstractmethod
    async def clear_override(self, owner_id: str, agent_id: str, key: str) -> bool:
        pass


class ISyncUserSettingsRepo(ABC):
    """The writer's synchronous face, for the owner's own chat command.

    The HTTP surface writes through `IUserSettingsRepo` on the async engine; the supervisor
    loop that handles a slash command has no loop of its own to await on, so it writes the
    same row through the synchronous engine the reader already reads.
    """

    @abstractmethod
    def set_override(self, owner_id: str, agent_id: str, key: str, value: str) -> None:
        """Replace the pair's stored value for this key."""


__all__ = [
    "IAsyncUserSettingsStore",
    "ISyncUserSettingsRepo",
    "IUserSettingsRepo",
    "UserSettingsStore",
]
