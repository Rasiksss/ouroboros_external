from abc import ABC, abstractmethod
from typing import Any


class ICache(ABC):
    @abstractmethod
    def get_value(self, key: str) -> Any:
        pass

    @abstractmethod
    def set_value(self, key: str, value: Any, ttl: int | None = None) -> None:
        pass

    @abstractmethod
    def delete_value(self, key: str) -> None:
        pass


class IAsyncCache(ABC):
    @abstractmethod
    async def get_value(self, key: str) -> Any:
        pass

    @abstractmethod
    async def set_value(self, key: str, value: Any, ttl: int | None = None) -> None:
        pass

    @abstractmethod
    async def delete_value(self, key: str) -> None:
        pass
