from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypedDict

from new_core.identity import OwnerScope
from ouroboros.storage.runtime_models import LlmUsageRecord


class UsageStats(TypedDict):
    spent_calls: int
    spent_tokens_prompt: int
    spent_tokens_completion: int
    spent_tokens_cached: int


class IUsageRepository(ABC):
    @abstractmethod
    def record_usage(self, usage: LlmUsageRecord) -> LlmUsageRecord:
        pass

    @abstractmethod
    def sum_usage_stats(self, *, session_id: str | None = None, scope: OwnerScope | None = None) -> UsageStats:
        pass
