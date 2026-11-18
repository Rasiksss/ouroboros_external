from abc import ABC, abstractmethod
from contextlib import AbstractContextManager

from ouroboros.storage.runtime_backend import TERMINAL_CLAIM_MAX_BATCH
from ouroboros.storage.runtime_models import TaskRecord


class IResultDeliveryRepository(ABC):
    @property
    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]: ...

    @abstractmethod
    def claim_terminal_jobs(self, *, limit: int = TERMINAL_CLAIM_MAX_BATCH) -> list[TaskRecord]:
        """Claim at most 50 terminal jobs; non-positive limits claim none."""
        ...

    @abstractmethod
    def terminal_publication_remaining(self, task_id: str, *, claim: str) -> float: ...

    @abstractmethod
    def record_terminal_delivery(self, task_id: str, *, claim: str, item: str, disposition: str) -> bool: ...

    @abstractmethod
    def finish_terminal_publication(
        self, task_id: str, *, claim: str, accepted: bool, retry_after_sec: int = 5
    ) -> bool: ...
