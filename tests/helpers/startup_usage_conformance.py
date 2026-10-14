from abc import ABC, abstractmethod

from new_core.identity import OwnerScope
from ouroboros.agent.interfaces.startup_usage import IStartupUsageRepository, StartupUsage
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with


class StartupUsageRepositoryConformance(ABC):
    @abstractmethod
    def make_repository(self) -> IStartupUsageRepository:
        pass

    @abstractmethod
    def seed(self, repository: IStartupUsageRepository, scope: OwnerScope, usage: StartupUsage) -> None:
        pass

    def test_missing_state_has_zero_counters(self) -> None:
        assert self.make_repository().get_usage(TEST_SCOPE) == StartupUsage()

    @parametrize_from_dict(
        {
            "other owner": {"scope": scope_with(TEST_SCOPE, owner_id="other"), "shared": False},
            "other agent": {"scope": scope_with(TEST_SCOPE, agent_id="other"), "shared": False},
            "other chat": {"scope": scope_with(TEST_SCOPE, chat_id="other"), "shared": True},
            "other surface": {"scope": scope_with(TEST_SCOPE, surface_id="dialogs"), "shared": True},
        }
    )
    def test_counters_are_addressed_by_owner_and_agent(self, scope: OwnerScope, shared: bool) -> None:
        repository = self.make_repository()
        usage = StartupUsage(spent_calls=7, spent_tokens_prompt=11, spent_tokens_completion=13, spent_tokens_cached=17)
        self.seed(repository, TEST_SCOPE, usage)

        assert repository.get_usage(scope) == (usage if shared else StartupUsage())

    def test_readers_get_independent_fresh_snapshots(self) -> None:
        repository = self.make_repository()
        self.seed(repository, TEST_SCOPE, StartupUsage(spent_calls=7))
        returned = repository.get_usage(TEST_SCOPE)
        returned.spent_calls = 99
        assert repository.get_usage(TEST_SCOPE).spent_calls == 7
        self.seed(repository, TEST_SCOPE, StartupUsage(spent_calls=11))
        assert repository.get_usage(TEST_SCOPE).spent_calls == 11
