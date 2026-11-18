"""Abstract serializer — translates storage objects into response schemas."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseSerializer(ABC):
    """Hooks invoked by `BaseCRUDService` after repo operations complete."""

    @abstractmethod
    async def get_list(self, objects: Any) -> Any: ...

    @abstractmethod
    async def get_single(self, obj: Any) -> Any: ...

    async def on_create(self, obj: Any) -> Any:
        return await self.get_single(obj)

    async def on_update(self, obj: Any) -> Any:
        return await self.get_single(obj)

    async def on_delete(self, result: Any) -> Any:
        return result
