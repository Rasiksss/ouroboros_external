from __future__ import annotations

from typing import Any

from infra.cache.interface import IAsyncCache, ICache


class NullCache(ICache):
    def get_value(self, key: str) -> Any:
        return None

    def set_value(self, key: str, value: Any, ttl: int | None = None) -> None:
        return None

    def delete_value(self, key: str) -> None:
        return None


class AsyncNullCache(IAsyncCache):
    async def get_value(self, key: str) -> Any:
        return None

    async def set_value(self, key: str, value: Any, ttl: int | None = None) -> None:
        return None

    async def delete_value(self, key: str) -> None:
        return None


__all__ = ["AsyncNullCache", "NullCache"]
