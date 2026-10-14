from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from ouroboros.storage.db_url import RUNTIME_DB_URL_KEY, collect_runtime_db_urls

if TYPE_CHECKING:
    from ouroboros.storage.settings.schema import AppSettings


def database_now(conn: Connection) -> datetime:
    value: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
    return value.astimezone(timezone.utc)


def runtime_engine_urls(db_urls: str | list[str]) -> list[str]:
    urls = [db_urls] if isinstance(db_urls, str) else [str(url) for url in db_urls]
    urls = [url for url in urls if url.strip()]
    if not urls:
        raise ValueError(
            f"no runtime database configured: set {RUNTIME_DB_URL_KEY}, or {RUNTIME_DB_URL_KEY}_1..N for a failover set"
        )
    if not all(url.startswith("postgresql") for url in urls):
        raise ValueError(
            f"the runtime database must be PostgreSQL: set {RUNTIME_DB_URL_KEY},"
            f" or {RUNTIME_DB_URL_KEY}_1..N for a failover set"
        )
    return urls


def refuse_without_runtime_database(settings: AppSettings) -> None:
    runtime_engine_urls(collect_runtime_db_urls(settings))


def pool_kwargs(*, pool_size: int | None, max_overflow: int | None) -> dict[str, Any]:
    # Settings composition imports engine setup before materializing the singleton.
    from ouroboros.storage.settings.store import load_settings

    settings = load_settings()
    kwargs: dict[str, Any] = {
        "pool_pre_ping": settings.OUROBOROS_DB_POOL_PRE_PING,
        "pool_timeout": settings.OUROBOROS_DB_POOL_TIMEOUT_SEC,
    }
    if pool_size is not None:
        kwargs["pool_size"] = max(1, int(pool_size))
    if max_overflow is not None:
        kwargs["max_overflow"] = max(0, int(max_overflow))
    return kwargs


def register_disconnect_handler(engine: Engine) -> None:
    @event.listens_for(engine, "handle_error")
    def _mark_disconnect(context: Any) -> None:
        exc = context.original_exception
        if exc is None:
            return
        if isinstance(exc, DBAPIError) and exc.connection_invalidated:
            context.is_disconnect = True
            return
        if isinstance(exc, (OSError, ConnectionError)):
            context.is_disconnect = True


def register_runtime_engine_listeners(engine: Engine) -> None:
    register_disconnect_handler(engine)


__all__ = [
    "pool_kwargs",
    "register_disconnect_handler",
    "register_runtime_engine_listeners",
    "runtime_engine_urls",
]
