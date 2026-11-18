from __future__ import annotations

import json
import logging
import time
from typing import Any

import redis
import redis.asyncio

from infra.cache.interface import IAsyncCache, ICache
from infra.redis_client import key_prefix

log = logging.getLogger(__name__)

FAILURE_WINDOW_SEC = 5.0


def _namespaced(key: str) -> str:
    return f"{key_prefix()}:{key}"


def _decoded(document: Any, key: str) -> Any:
    if document is None:
        return None
    try:
        return json.loads(document)
    except ValueError:
        log.warning("The cache holds a value under %s that is not JSON; the caller reads its own source", key)
        return None


class _HoldOff:
    def __init__(self) -> None:
        self._silent_until = 0.0

    def holding(self) -> bool:
        return time.monotonic() < self._silent_until

    def begin(self, half: str) -> None:
        self._silent_until = time.monotonic() + FAILURE_WINDOW_SEC
        log.warning(
            "The cache is unreachable on %s; the caller reads its own source for the next %.0fs",
            half,
            FAILURE_WINDOW_SEC,
            exc_info=True,
        )


class RedisCache(ICache):
    def __init__(self, client: redis.Redis, *, ttl_sec: int) -> None:
        self._client = client
        self._ttl_sec = ttl_sec
        self._hold_off = _HoldOff()

    def get_value(self, key: str) -> Any:
        if self._hold_off.holding():
            return None
        try:
            document = self._client.get(_namespaced(key))
        except (redis.RedisError, OSError):
            self._hold_off.begin("read")
            return None
        return _decoded(document, key)

    def set_value(self, key: str, value: Any, ttl: int | None = None) -> None:
        if self._hold_off.holding():
            return
        try:
            self._client.set(_namespaced(key), json.dumps(value), ex=self._ttl_sec if ttl is None else ttl)
        except (redis.RedisError, OSError):
            self._hold_off.begin("write")

    def delete_value(self, key: str) -> None:
        self._client.delete(_namespaced(key))


class AsyncRedisCache(IAsyncCache):
    def __init__(self, client: redis.asyncio.Redis, *, ttl_sec: int) -> None:
        self._client = client
        self._ttl_sec = ttl_sec
        self._hold_off = _HoldOff()

    async def get_value(self, key: str) -> Any:
        if self._hold_off.holding():
            return None
        try:
            document = await self._client.get(_namespaced(key))
        except (redis.RedisError, OSError):
            self._hold_off.begin("read")
            return None
        return _decoded(document, key)

    async def set_value(self, key: str, value: Any, ttl: int | None = None) -> None:
        if self._hold_off.holding():
            return
        try:
            await self._client.set(_namespaced(key), json.dumps(value), ex=self._ttl_sec if ttl is None else ttl)
        except (redis.RedisError, OSError):
            self._hold_off.begin("write")

    async def delete_value(self, key: str) -> None:
        await self._client.delete(_namespaced(key))


__all__ = ["FAILURE_WINDOW_SEC", "AsyncRedisCache", "RedisCache"]
