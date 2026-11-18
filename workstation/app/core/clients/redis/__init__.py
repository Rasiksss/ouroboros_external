"""Async Redis client with a cache-aside helper.

Same pattern as `AuthClient`: class + module-level instance, lazy init, own
settings in `config.py`, no shared base class.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from redis.asyncio import Redis, from_url

from app.core.clients.redis.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RedisClient:
    def __init__(self) -> None:
        self._redis: Redis | None = None

    async def _get_client(self) -> Redis:
        if self._redis is None:
            logger.info("Initialize Redis client url=%s", settings.REDIS_URL)
            self._redis = from_url(settings.REDIS_URL, decode_responses=False)
        return self._redis

    async def start(self) -> None:
        """Eager ping (optional): called from lifespan when `REDIS_ENABLED=True`."""
        if not settings.REDIS_ENABLED:
            return
        client = await self._get_client()
        await client.ping()

    async def stop(self) -> None:
        await self.close()

    async def close(self) -> None:
        if self._redis is not None:
            logger.info("Close Redis client")
            await self._redis.aclose()
            self._redis = None

    async def ping(self) -> bool:
        if not settings.REDIS_ENABLED:
            return False
        client = await self._get_client()
        return bool(await client.ping())

    async def get(self, key: str) -> Any | None:
        if not settings.REDIS_ENABLED:
            return None
        client = await self._get_client()
        raw = await client.get(key)
        return json.loads(raw) if raw is not None else None

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        if not settings.REDIS_ENABLED:
            return
        client = await self._get_client()
        payload = json.dumps(value, default=str, ensure_ascii=False).encode("utf-8")
        await client.set(key, payload, ex=ttl or settings.REDIS_DEFAULT_TTL)

    async def delete(self, *keys: str) -> int:
        if not settings.REDIS_ENABLED or not keys:
            return 0
        client = await self._get_client()
        return int(await client.delete(*keys))

    async def cache(
        self,
        key: str,
        producer: Callable[[], Awaitable[T]],
        ttl: int | None = None,
    ) -> T:
        """Cache-aside: return cached value or compute via `producer` and store."""
        cached = await self.get(key)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        value = await producer()
        await self.set(key, value, ttl=ttl)
        return value

    @staticmethod
    def build_key(*parts: Any) -> str:
        """Stable cache key from arbitrary args (hashes long tails)."""
        raw = ":".join(str(p) for p in parts)
        if len(raw) <= 200:
            return raw
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"{raw[:150]}:{digest[:16]}"


redis_client = RedisClient()
