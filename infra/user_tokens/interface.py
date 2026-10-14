from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class UserTokenTargetKind(StrEnum):
    MCP_SERVER = "mcp-server"
    A2A_AGENT = "a2a-agent"


@dataclass(frozen=True)
class UserTokenTarget:
    kind: UserTokenTargetKind
    target_id: str


class IUserTokenStore(ABC):
    @abstractmethod
    async def load(self, target: UserTokenTarget, owner_id: str, agent_id: str) -> str | None:
        pass


class IUserTokenRepo(ABC):
    @abstractmethod
    async def save(
        self, target: UserTokenTarget, owner_id: str, agent_id: str, token: str, expires_at: datetime
    ) -> None:
        pass

    @abstractmethod
    async def clear(self, target: UserTokenTarget, owner_id: str, agent_id: str) -> bool:
        pass

    @abstractmethod
    async def expirations(self, kind: UserTokenTargetKind, owner_id: str, agent_id: str) -> dict[str, datetime]:
        pass


__all__ = ["IUserTokenRepo", "IUserTokenStore", "UserTokenTarget", "UserTokenTargetKind"]
