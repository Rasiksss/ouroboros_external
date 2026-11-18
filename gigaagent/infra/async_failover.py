from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import psycopg

from ouroboros.storage.db_url import FailoverRotation, postgres_connect_kwargs


def async_postgres_failover_creator(urls: list[str]) -> Callable[[], Awaitable[Any]]:
    if len(urls) < 2:
        raise ValueError("async_postgres_failover_creator requires at least two URLs")
    rotation = FailoverRotation(len(urls))

    async def connect() -> Any:
        last_exc: BaseException | None = None
        for index in rotation.order():
            try:
                connection = await psycopg.AsyncConnection.connect(**postgres_connect_kwargs(urls[index]))
            except Exception as exc:
                last_exc = exc
                continue
            rotation.last_good_index = index
            return connection
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("no PostgreSQL URLs configured for failover")

    return connect


__all__ = ["async_postgres_failover_creator"]
