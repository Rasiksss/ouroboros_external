from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from typing import TypeAlias

from new_core.identity import OwnerScope
from ouroboros.storage.runtime_models import ToolConfirmation

ConfirmationSnapshot: TypeAlias = dict[str, object]
ConfirmationArguments: TypeAlias = dict[str, object]


class IToolConfirmationRepository(ABC):
    @abstractmethod
    def read_tool_confirmation_status(self, confirmation_id: str) -> str:
        pass

    @abstractmethod
    def read_open_tool_confirmation(self, scope: OwnerScope) -> ToolConfirmation | None:
        pass

    @abstractmethod
    def read_tool_confirmation_decisions(self, task_id: str, *, round_idx: int) -> dict[tuple[int, str, str], str]:
        pass

    @property
    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]:
        pass

    @abstractmethod
    def park_and_open(
        self,
        *,
        task_id: str,
        claim_token: str,
        confirmation: ToolConfirmation,
        snapshot: ConfirmationSnapshot,
        workspace: str,
        arguments: ConfirmationArguments,
    ) -> ToolConfirmation | None: ...

    @abstractmethod
    def unpark_task(self, confirmation_id: str, *, now_iso: str) -> bool: ...

    @abstractmethod
    def requeue_parked_task(self, task_id: str) -> bool: ...

    @abstractmethod
    def take_parked_snapshot(self, task_id: str) -> ConfirmationSnapshot | None: ...

    @abstractmethod
    def spend_round_decisions(self, task_id: str, *, round_idx: int, now_iso: str) -> int: ...

    @abstractmethod
    def list_parked_tasks_without_pending_request(self, *, limit: int = 100) -> list[str]: ...

    @abstractmethod
    def close_confirmations_nobody_is_parked_on(self, *, now_iso: str) -> list[ToolConfirmation]: ...

    @abstractmethod
    def forget_snapshots_of_finished_runs(self, *, limit: int = 100) -> int: ...

    @abstractmethod
    def resolve_tool_confirmation(self, confirmation_id: str, *, decision: str, now_iso: str) -> bool: ...

    @abstractmethod
    def expire_tool_confirmation(self, confirmation_id: str, *, now_iso: str) -> bool: ...
