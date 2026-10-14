from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextlib import AbstractContextManager

from new_core.identity import OwnerScope
from ouroboros.storage.runtime_models import TaskEpisode, ToolInvocation


class IExecutionMemoryRepository(ABC):
    @property
    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]: ...

    @abstractmethod
    def record_task_episode(self, episode: TaskEpisode) -> TaskEpisode: ...

    @abstractmethod
    def list_task_episodes(
        self,
        *,
        scope: OwnerScope,
        domains: Iterable[str] | None = None,
        search_terms: Iterable[str] | None = None,
        limit: int = 100,
        snapshot: dict[str, str] | None = None,
    ) -> list[TaskEpisode]: ...

    @abstractmethod
    def record_tool_invocation(self, invocation: ToolInvocation) -> ToolInvocation:
        pass

    @abstractmethod
    def list_tool_invocations(self, *, task_id: str, limit: int = 100) -> list[ToolInvocation]:
        pass
