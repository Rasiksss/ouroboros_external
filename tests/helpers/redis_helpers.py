"""Redis test helpers extracted from test_transport_redis."""

from __future__ import annotations

import os
import uuid

TEST_REDIS_URL = os.environ.get("OUROBOROS_TEST_REDIS_URL", "")
_MINTED: list[str] = []


def redis_client():
    import redis

    return redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True, socket_connect_timeout=5.0, socket_timeout=10.0)


def fresh_prefix() -> str:
    prefix = f"ouroboros-test:{uuid.uuid4().hex}"
    _MINTED.append(prefix)
    return prefix
