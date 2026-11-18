"""Abstract CRUD repository — storage-agnostic contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseCRUDRepository(ABC):
    """Every concrete repo (SQLA, in-memory, mock) implements this surface."""

    @abstractmethod
    async def get_single(self, **conditions: Any) -> Any: ...

    @abstractmethod
    async def get_list(self, **conditions: Any) -> list[Any]: ...

    @abstractmethod
    async def create(self, **data: Any) -> Any: ...

    @abstractmethod
    async def update(self, object_id: Any, **data: Any) -> Any: ...

    @abstractmethod
    async def delete(self, object_id: Any, **kwargs: Any) -> bool: ...
