"""Health probes — use from `/health/liveness` and `/health/readiness` endpoints."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clients.redis import redis_client
from app.core.clients.redis.config import settings as redis_settings

logger = logging.getLogger("healthz")


class DatabaseInaccessibilityError(Exception):
    detail = "DB is currently inaccessible."


class RedisInaccessibilityError(Exception):
    detail = "Redis is currently inaccessible."


async def probe_db(session: AsyncSession) -> None:
    """Ping the database with `SELECT 1`."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.critical("DB probe failed: %s", exc, exc_info=exc)
        raise DatabaseInaccessibilityError from exc


async def probe_redis() -> None:
    """Ping Redis (no-op when disabled)."""
    if not redis_settings.REDIS_ENABLED:
        return
    try:
        await redis_client.ping()
    except Exception as exc:
        logger.critical("Redis probe failed: %s", exc, exc_info=exc)
        raise RedisInaccessibilityError from exc
