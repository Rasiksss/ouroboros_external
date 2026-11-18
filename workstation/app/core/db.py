"""Async SQLAlchemy engine, session factory and FastAPI dependency.

Session lifecycle rule (INVARIANT): commit is performed ONLY by `get_session`
dependency on success; repositories/services use `session.flush()` to obtain
generated ids but never `commit()`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

_SQLITE_BUSY_TIMEOUT_MS = 5000
_SQLITE_CONNECT_TIMEOUT_SEC = 30.0


def _apply_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Enable WAL + busy_timeout so concurrent requests don't hit 'database is locked'."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


def init_engine() -> AsyncEngine:
    """Create the global async engine and session factory (idempotent)."""
    global _engine, _session_maker
    if _engine is None:
        engine_kwargs: dict[str, Any] = {
            "echo": settings.DATABASE_ECHO,
            "pool_pre_ping": True,
        }
        is_sqlite = settings.DATABASE_URL.startswith("sqlite+")
        if is_sqlite:
            # aiosqlite's default 5s timeout still trips under concurrent writers;
            # raise it and pair with PRAGMA busy_timeout below.
            engine_kwargs["connect_args"] = {"timeout": _SQLITE_CONNECT_TIMEOUT_SEC}
        else:
            engine_kwargs["pool_size"] = settings.DATABASE_POOL_SIZE
            engine_kwargs["max_overflow"] = settings.DATABASE_MAX_OVERFLOW
        _engine = create_async_engine(
            settings.DATABASE_URL,
            **engine_kwargs,
        )
        if is_sqlite:
            event.listen(_engine.sync_engine, "connect", _apply_sqlite_pragmas)
        _session_maker = async_sessionmaker(
            _engine,
            expire_on_commit=False,
            autoflush=False,
        )
    return _engine


async def dispose_engine() -> None:
    """Close all pooled connections (call on shutdown)."""
    global _engine, _session_maker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_maker = None


def get_engine() -> AsyncEngine:
    if _engine is None:
        return init_engine()
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    if _session_maker is None:
        init_engine()
    assert _session_maker is not None
    return _session_maker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: transactional async session.

    On success -> commit; on exception -> rollback; always closed.
    """
    maker = get_session_maker()
    async with maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
