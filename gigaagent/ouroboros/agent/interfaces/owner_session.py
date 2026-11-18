from abc import ABC, abstractmethod

from new_core.identity import OwnerScope


class IOwnerSessionRepository(ABC):
    @abstractmethod
    def get_session_id(self, scope: OwnerScope) -> str:
        pass
