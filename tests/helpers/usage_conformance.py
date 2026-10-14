from __future__ import annotations

from abc import ABC, abstractmethod

from new_core.identity import OwnerScope
from ouroboros.storage.runtime_models import LlmUsageRecord
from supervisor.interfaces.usage import IUsageRepository
from tests.helpers.parametrize import parametrize_from_dict

SCOPE = OwnerScope(owner_id="owner", agent_id="agent", surface_id="browser", chat_id="1")
OTHER_AGENT = OwnerScope(owner_id="owner", agent_id="other", surface_id="browser", chat_id="1")
OTHER_OWNER = OwnerScope(owner_id="other", agent_id="agent", surface_id="browser", chat_id="1")


class UsageRepositoryConformance(ABC):
    @abstractmethod
    def make_repository(self) -> IUsageRepository:
        pass

    def test_empty_stats_are_zero(self) -> None:
        assert self.make_repository().sum_usage_stats(scope=SCOPE) == {
            "spent_calls": 0,
            "spent_tokens_prompt": 0,
            "spent_tokens_completion": 0,
            "spent_tokens_cached": 0,
        }

    @parametrize_from_dict(
        {
            "owner and agent": {"scope": SCOPE, "session_id": None, "tokens": 3, "calls": 2},
            "owner agent and session": {"scope": SCOPE, "session_id": "a", "tokens": 1, "calls": 1},
            "other agent": {"scope": OTHER_AGENT, "session_id": None, "tokens": 4, "calls": 1},
            "other owner": {"scope": OTHER_OWNER, "session_id": None, "tokens": 8, "calls": 1},
            "operator session": {"scope": None, "session_id": "a", "tokens": 13, "calls": 3},
            "operator total": {"scope": None, "session_id": None, "tokens": 15, "calls": 4},
            "missing session": {"scope": SCOPE, "session_id": "missing", "tokens": 0, "calls": 0},
        }
    )
    def test_totals_respect_owner_agent_and_session(self, scope, session_id, tokens, calls) -> None:
        repository = self.make_repository()
        for owner, session, amount in ((SCOPE, "a", 1), (SCOPE, "b", 2), (OTHER_AGENT, "a", 4), (OTHER_OWNER, "a", 8)):
            repository.record_usage(
                LlmUsageRecord(
                    owner_id=owner.owner_id,
                    agent_id=owner.agent_id,
                    prompt_tokens=amount,
                    completion_tokens=amount * 2,
                    cached_tokens=amount * 3,
                    metadata={"session_id": session},
                )
            )

        assert repository.sum_usage_stats(scope=scope, session_id=session_id) == {
            "spent_calls": calls,
            "spent_tokens_prompt": tokens,
            "spent_tokens_completion": tokens * 2,
            "spent_tokens_cached": tokens * 3,
        }

    def test_record_fills_identity_and_time_and_keeps_a_snapshot(self) -> None:
        repository = self.make_repository()
        record = LlmUsageRecord(owner_id=SCOPE.owner_id, agent_id=SCOPE.agent_id, prompt_tokens=17)

        assert repository.record_usage(record) is record
        assert record.usage_id
        assert record.created_at
        record.prompt_tokens = 99
        record.metadata["session_id"] = "changed"

        assert repository.sum_usage_stats(scope=SCOPE)["spent_tokens_prompt"] == 17
        assert repository.sum_usage_stats(scope=SCOPE, session_id="changed")["spent_calls"] == 0
