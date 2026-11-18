from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass

from new_core.identity import OwnerScope

CLEANUP_CHECKPOINT_KEY = "working_retrieval_cleanup"


@dataclass
class CleanupCheckpoint:
    last_run_at: str
    removed: dict[str, int]


class ICleanupCheckpointRepository(ABC):
    @abstractmethod
    def try_claim(self) -> AbstractContextManager[bool]:
        pass

    @abstractmethod
    def get_last_run_at(self, scope: OwnerScope) -> str:
        pass

    @abstractmethod
    def save_checkpoint(self, checkpoint: CleanupCheckpoint, scope: OwnerScope) -> None:
        pass
