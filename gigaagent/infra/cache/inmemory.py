from __future__ import annotations

import time
from typing import Any

from infra.cache.interface import IAsyncCache, ICache


class InMemoryCache(ICache):
    def __init__(self, *, ttl_sec: int) -> None:
        self._ttl_sec = ttl_sec
        self._entries: dict[str, tuple[float, Any]] = {}

    def get_value(self, key: str) -> Any:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return None
        return value

    def set_value(self, key: str, value: Any, ttl: int | None = None) -> None:
        self._entries[key] = (time.monotonic() + (self._ttl_sec if ttl is None else ttl), value)

    def delete_value(self, key: str) -> None:
        self._entries.pop(key, None)


class AsyncInMemoryCache(IAsyncCache):
    """The async face of one `InMemoryCache`, for the process where reader and writer are the same.

    There is no I/O to move off the event loop here — the entries are a dict — so this delegates
    rather than hops a thread. What it exists for is that both faces must see the same entries.
    """

    def __init__(self, entries: InMemoryCache) -> None:
        self._entries = entries

    async def get_value(self, key: str) -> Any:
        return self._entries.get_value(key)

    async def set_value(self, key: str, value: Any, ttl: int | None = None) -> None:
        self._entries.set_value(key, value, ttl)

    async def delete_value(self, key: str) -> None:
        self._entries.delete_value(key)


__all__ = ["AsyncInMemoryCache", "InMemoryCache"]
