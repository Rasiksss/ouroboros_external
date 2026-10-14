from __future__ import annotations

from infra.cache.inmemory import AsyncInMemoryCache, InMemoryCache
from infra.cache.interface import IAsyncCache, ICache
from infra.cache.null import AsyncNullCache, NullCache
from ouroboros.storage.settings.store import load_settings

CONNECT_TIMEOUT_SEC = 0.25
SOCKET_TIMEOUT_SEC = 0.25


def refuse_a_process_local_settings_cache(component: str) -> None:
    if str(load_settings().OUROBOROS_SETTINGS_CACHE_BACKEND or "none") != "inmemory":
        return
    raise RuntimeError(
        f"OUROBOROS_SETTINGS_CACHE_BACKEND=inmemory in {component}: this process is a component "
        "tier, so the owner-settings writer runs in another process and never reaches a cache held "
        "in this one — every reader would keep resolving the value an owner changed, for as long as "
        "the process lives, while the surface reports the change stored. Set "
        "OUROBOROS_SETTINGS_CACHE_BACKEND=redis (with OUROBOROS_REDIS_URL), or 'none' to read the "
        "store on every accessor call."
    )


class CacheFactory:
    """Builds and memoises the caches this process composes, one per backend.

    A backend is composed once and reused: a second `redis` composition would open a second
    connection pool per request, and the in-process backend must be one set of entries across
    both of its call styles, since the process that composes `inmemory` is the one process
    where the reader and the writer are the same.
    """

    def __init__(self) -> None:
        self._cache: ICache | None = None
        self._async_cache: IAsyncCache | None = None

    def initialize(self, backend: str, ttl_sec: int) -> None:
        if backend == "none":
            self._cache = NullCache()
            self._async_cache = AsyncNullCache()
        elif backend == "inmemory":
            self._cache = InMemoryCache(ttl_sec=ttl_sec)
            self._async_cache = AsyncInMemoryCache(self._cache)
        elif backend == "redis":
            if not str(load_settings().OUROBOROS_REDIS_URL or "").strip():
                raise RuntimeError(
                    "OUROBOROS_SETTINGS_CACHE_BACKEND=redis needs OUROBOROS_REDIS_URL, which is empty. "
                    "Set the URL, or put OUROBOROS_SETTINGS_CACHE_BACKEND back to 'none': composing no "
                    "cache reads the store on every accessor call, which is correct in every profile."
                )
            from infra.cache.redis_cache import AsyncRedisCache, RedisCache
            from infra.redis_client import async_redis_client, redis_client

            client = redis_client(retries=0, connect_timeout=CONNECT_TIMEOUT_SEC, socket_timeout=SOCKET_TIMEOUT_SEC)
            self._cache = RedisCache(client, ttl_sec=ttl_sec)
            async_client = async_redis_client(
                retries=0, connect_timeout=CONNECT_TIMEOUT_SEC, socket_timeout=SOCKET_TIMEOUT_SEC
            )
            self._async_cache = AsyncRedisCache(async_client, ttl_sec=ttl_sec)
        else:
            raise RuntimeError(f"OUROBOROS_SETTINGS_CACHE_BACKEND={backend} is not a backend: none, inmemory, redis")

    def cache(self) -> ICache:
        if not self._cache:
            raise RuntimeError("CacheFactory not initialized: call initialize() first")
        return self._cache

    def async_cache(self) -> IAsyncCache:
        if not self._async_cache:
            raise RuntimeError("CacheFactory not initialized: call initialize() first")
        return self._async_cache


cache_factory = CacheFactory()


__all__ = [
    "CONNECT_TIMEOUT_SEC",
    "SOCKET_TIMEOUT_SEC",
    "CacheFactory",
    "cache_factory",
    "refuse_a_process_local_settings_cache",
]
