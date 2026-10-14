from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from infra.async_engine import async_driver_url
from infra.async_failover import async_postgres_failover_creator
from ouroboros.storage.db_url import canonicalize_runtime_db_urls, collect_runtime_db_urls
from ouroboros.storage.engine_setup import runtime_engine_urls
from ouroboros.storage.settings.schema import AppSettings

log = logging.getLogger(__name__)

PROBE_WAIT_SEC = 2


class ProbeDatabase:
    """Its own connection per probe, one deadline over the whole dial."""

    def __init__(self, engine: AsyncEngine, *, deadline_sec: float) -> None:
        self.engine = engine
        self.deadline_sec = deadline_sec
        self._releases: set[asyncio.Task[None]] = set()

    async def dial(self) -> None:
        deadline = time.monotonic() + self.deadline_sec
        opening: asyncio.Task[AsyncConnection] = asyncio.ensure_future(self.engine.connect())
        waiting: asyncio.Task[Any] = opening
        connection: AsyncConnection | None = None
        try:
            if not await _within(opening, deadline):
                raise TimeoutError(f"no connection within {self.deadline_sec}s")
            connection = opening.result()
            waiting = asyncio.ensure_future(connection.execute(text("SELECT 1")))
            if not await _within(waiting, deadline):
                raise TimeoutError(f"no answer within {self.deadline_sec}s")
            waiting.result()
        finally:
            if not waiting.done():
                self._release(waiting, connection)
            elif connection is not None:
                await connection.close()

    async def dispose(self) -> None:
        await self.engine.dispose()

    def _release(self, attempt: asyncio.Task[Any], connection: AsyncConnection | None) -> None:
        released = asyncio.ensure_future(_close_behind(attempt, connection))
        self._releases.add(released)
        released.add_done_callback(self._releases.discard)
        released.add_done_callback(_collect_error)


async def _within(attempt: asyncio.Task[Any], deadline: float) -> bool:
    done, _ = await asyncio.wait({attempt}, timeout=max(0.0, deadline - time.monotonic()))
    return bool(done)


async def _close_behind(attempt: asyncio.Task[Any], connection: AsyncConnection | None) -> None:
    """The order matters, and the probe may not wait for it (`.rules/observability.md`)."""
    attempt.cancel()
    await asyncio.sleep(0)
    if connection is not None:
        await connection.invalidate()
        attempt.cancel()
    await asyncio.gather(attempt, return_exceptions=True)


def _collect_error(released: asyncio.Task[None]) -> None:
    if released.cancelled():
        return
    error = released.exception()
    if error is not None:
        log.debug("A readiness dial could not be released: %s", error)


def _with_connect_budget(url: str) -> str:
    budget = {"connect_timeout": str(PROBE_WAIT_SEC)}
    return make_url(url).update_query_dict(budget).render_as_string(hide_password=False)


def create_probe_database(db_urls: str | list[str]) -> ProbeDatabase:
    urls = [_with_connect_budget(url) for url in runtime_engine_urls(db_urls)]
    engine_kwargs: dict[str, Any] = {"isolation_level": "AUTOCOMMIT", "poolclass": NullPool}
    if len(urls) == 1:
        engine = create_async_engine(async_driver_url(urls[0]), **engine_kwargs)
    else:
        engine = create_async_engine(
            "postgresql+psycopg://", async_creator=async_postgres_failover_creator(urls), **engine_kwargs
        )
    return ProbeDatabase(engine, deadline_sec=PROBE_WAIT_SEC * (len(urls) + 1))


class ProbeDatabaseFactory:
    def __init__(self) -> None:
        self._database: ProbeDatabase | None = None

    def initialize_beside_store(self, settings: AppSettings) -> ProbeDatabase:
        if self._database is None:
            self._database = create_probe_database(canonicalize_runtime_db_urls(collect_runtime_db_urls(settings)))
        return self._database

    @property
    def database(self) -> ProbeDatabase:
        if self._database is None:
            raise RuntimeError(
                "the readiness probe's database is not composed: a composition root must call"
                " probe_database_factory.initialize_beside_store() before it serves its probes"
            )
        return self._database

    async def dispose(self) -> None:
        database, self._database = self._database, None
        if database is not None:
            await database.dispose()


probe_database_factory = ProbeDatabaseFactory()
__all__ = ["PROBE_WAIT_SEC", "ProbeDatabase", "ProbeDatabaseFactory", "create_probe_database", "probe_database_factory"]
