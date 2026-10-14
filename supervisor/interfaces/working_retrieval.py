from abc import ABC, abstractmethod


class IWorkingRetrievalRepository(ABC):
    @abstractmethod
    def cleanup_expired_working_retrieval(
        self, *, now_iso: str, limit: int = 5000, cleanup_retrieval: bool = True
    ) -> dict[str, int]:
        pass
