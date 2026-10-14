from abc import ABC, abstractmethod

from ouroboros.storage.runtime_models import RuntimeSession


class IRuntimeSessionRepository(ABC):
    @abstractmethod
    def create_or_update_session(self, session: RuntimeSession) -> RuntimeSession:
        pass
