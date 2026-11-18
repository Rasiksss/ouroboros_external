"""Construction of the synchronous runtime database engine."""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import Pool

from ouroboros.storage.db_url import postgres_failover_creator
from ouroboros.storage.engine_setup import pool_kwargs, register_runtime_engine_listeners, runtime_engine_urls


def create_runtime_engine(
    db_urls: str | list[str],
    *,
    poolclass: type[Pool] | None = None,
    pool_size: int | None = None,
    max_overflow: int | None = None,
) -> Engine:
    """Build the runtime engine with pool limits chosen by the composition root."""
    urls = runtime_engine_urls(db_urls)
    engine_kwargs: dict[str, Any] = pool_kwargs(pool_size=pool_size, max_overflow=max_overflow)
    if poolclass is not None:
        engine_kwargs = {"pool_pre_ping": True, "poolclass": poolclass}

    if len(urls) == 1:
        engine = create_engine(urls[0], **engine_kwargs)
    else:
        engine = create_engine(
            "postgresql+psycopg://",
            creator=postgres_failover_creator(urls),
            **engine_kwargs,
        )

    register_runtime_engine_listeners(engine)
    return engine
