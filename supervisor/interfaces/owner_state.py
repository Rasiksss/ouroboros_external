from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import JsonValue

from new_core.identity import OwnerScope


@dataclass
class OwnerStateDocument:
    payload: dict[str, JsonValue]


class IOwnerStateRepository(ABC):
    @abstractmethod
    def load(self, scope: OwnerScope) -> OwnerStateDocument | None:
        pass

    @abstractmethod
    def save(self, document: OwnerStateDocument, scope: OwnerScope) -> None:
        pass
