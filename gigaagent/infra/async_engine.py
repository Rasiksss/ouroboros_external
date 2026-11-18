from __future__ import annotations

from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from infra.async_failover import async_postgres_failover_creator
from ouroboros.storage.db_url import canonicalize_runtime_db_urls, collect_runtime_db_urls
from ouroboros.storage.engine_setup import pool_kwargs, register_runtime_engine_listeners, runtime_engine_urls
from ouroboros.storage.settings.schema import AppSettings

ASYNC_POOL_SIZE = 4
ASYNC_MAX_OVERFLOW = 4


def async_driver_url(url: str) -> str:
    parsed = make_url(url)
    if parsed.get_backend_name() == "postgresql" and parsed.get_driver_name() != "psycopg":
        return parsed.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
    return url


def create_async_runtime_engine(
    db_urls: str | list[str], *, pool_size: int = ASYNC_POOL_SIZE, max_overflow: int = ASYNC_MAX_OVERFLOW
) -> AsyncEngine:
    urls = runtime_engine_urls(db_urls)
    engine_kwargs: dict[str, Any] = pool_kwargs(pool_size=pool_size, max_overflow=max_overflow)
    if len(urls) == 1:
        engine = create_async_engine(async_driver_url(urls[0]), **engine_kwargs)
    else:
        engine = create_async_engine(
            "postgresql+psycopg://", async_creator=async_postgres_failover_creator(urls), **engine_kwargs
        )
    register_runtime_engine_listeners(engine.sync_engine)
    return engine


class AsyncEngineFactory:
    def __init__(self) -> None:
        self._urls: list[str] = []
        self._pool_size = ASYNC_POOL_SIZE
        self._max_overflow = ASYNC_MAX_OVERFLOW
        self._engine: AsyncEngine | None = None

    def initialize(
        self, db_urls: str | list[str], *, pool_size: int = ASYNC_POOL_SIZE, max_overflow: int = ASYNC_MAX_OVERFLOW
    ) -> None:
        urls = runtime_engine_urls(db_urls)
        if self._engine is not None:
            raise RuntimeError("the async runtime DB engine is already built; changing it is a restart, not a call")
        self._urls = urls
        self._pool_size = pool_size
        self._max_overflow = max_overflow

    def initialize_beside_store(self, settings: AppSettings) -> AsyncEngine:
        self.initialize(
            canonicalize_runtime_db_urls(collect_runtime_db_urls(settings)),
            pool_size=settings.OUROBOROS_DB_ASYNC_POOL_SIZE,
            max_overflow=settings.OUROBOROS_DB_ASYNC_MAX_OVERFLOW,
        )
        return self.engine

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            if not self._urls:
                raise RuntimeError(
                    "the async runtime DB engine is not composed: a composition root must"
                    " call async_engine_factory.initialize(urls) before the agent registry serves"
                )
            self._engine = create_async_runtime_engine(
                self._urls, pool_size=self._pool_size, max_overflow=self._max_overflow
            )
        return self._engine

    async def dispose(self) -> None:
        (engine, self._engine) = (self._engine, None)
        self._urls = []
        if engine is not None:
            await engine.dispose()


async_engine_factory = AsyncEngineFactory()
__all__ = [
    "ASYNC_MAX_OVERFLOW",
    "ASYNC_POOL_SIZE",
    "AsyncEngineFactory",
    "async_driver_url",
    "async_engine_factory",
    "create_async_runtime_engine",
]
