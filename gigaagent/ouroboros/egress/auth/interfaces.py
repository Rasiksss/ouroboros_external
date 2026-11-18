from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from infra.security_context.interface import ISecurityContextStore
from infra.user_tokens.interface import IUserTokenStore
from new_core.identity import OwnerScope


class EgressAuthRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class EgressAuthDependencies:
    security_context_store: ISecurityContextStore
    user_token_store: IUserTokenStore


class IEgressAuthenticator(ABC):
    @property
    @abstractmethod
    def forwarded_headers(self) -> frozenset[str]:
        pass

    @abstractmethod
    async def request_headers(self, scope: OwnerScope) -> dict[str, str]:
        pass


__all__ = ["EgressAuthDependencies", "EgressAuthRefused", "IEgressAuthenticator"]
