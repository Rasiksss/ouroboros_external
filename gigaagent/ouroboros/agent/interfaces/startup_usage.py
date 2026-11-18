from abc import ABC, abstractmethod

from pydantic import BaseModel

from new_core.identity import OwnerScope


class StartupUsage(BaseModel):
    spent_calls: int = 0
    spent_tokens_prompt: int = 0
    spent_tokens_completion: int = 0
    spent_tokens_cached: int = 0


class IStartupUsageRepository(ABC):
    @abstractmethod
    def get_usage(self, scope: OwnerScope) -> StartupUsage:
        pass
