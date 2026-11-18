"""Clients registry + lifecycle helpers.

Pattern: every client is a subpackage under `app/core/clients/<name>/` with:
  - `config.py` — its own `BaseSettings` (local env vars)
  - `__init__.py` — the client class + a module-level instance (`<name>_client`)

Both `start_all_clients()` and `close_all_clients()` are called from lifespan.
When you add a new client, import its instance here and add it to both helpers.
"""

from __future__ import annotations

from app.core.clients.auth import auth_client
from app.core.clients.kafka import kafka_client
from app.core.clients.redis import redis_client

__all__ = [
    "auth_client",
    "close_all_clients",
    "kafka_client",
    "redis_client",
    "start_all_clients",
]


async def start_all_clients() -> None:
    """Eager startup for clients that need it (network ping, open pools)."""
    await redis_client.start()
    await kafka_client.start()


async def close_all_clients() -> None:
    """Close every client. Safe to call even if they were never started."""
    await auth_client.close()
    await kafka_client.close()
    await redis_client.close()
