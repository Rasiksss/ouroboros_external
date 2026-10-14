from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlsplit

import redis
import redis.asyncio
from redis.asyncio.retry import Retry as AsyncRetry
from redis.asyncio.sentinel import Sentinel as AsyncSentinel
from redis.backoff import NoBackoff
from redis.retry import Retry
from redis.sentinel import Sentinel

from ouroboros.storage.settings.store import load_settings

BROKER_CONNECT_TIMEOUT_SEC = 5.0
BROKER_SOCKET_TIMEOUT_SEC = 10.0

HEALTH_CHECK_SEC = 30

SENTINEL_SCHEMES = frozenset({"sentinel", "redis-sentinel", "rediss-sentinel"})


def key_prefix() -> str:
    return str(load_settings().OUROBOROS_REDIS_PREFIX or "ouroboros").strip()


def parse_sentinel_url(url: str) -> dict[str, Any]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    ssl = scheme == "rediss-sentinel"
    hosts = parsed.netloc.rsplit("@", 1)[-1]
    addresses = [_split_sentinel_address(addr) for addr in hosts.split(",") if addr]
    if not addresses:
        raise RuntimeError("OUROBOROS_REDIS_URL sentinel URL lists no sentinel addresses")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    service_name = str(query.pop("service_name", "") or "").strip()
    if not service_name:
        raise RuntimeError(
            "OUROBOROS_REDIS_URL sentinel URL needs ?service_name=<master name>, e.g. "
            "sentinel://host:26379?service_name=mymaster"
        )
    db = int(query.pop("db", 0) or 0)
    return {
        "sentinels": addresses,
        "service_name": service_name,
        "db": db,
        "ssl": ssl,
        "username": parsed.username,
        "password": parsed.password,
        "kwargs": query,
    }


def _split_sentinel_address(addr: str) -> tuple[str, int]:
    host, sep, port = addr.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise RuntimeError(f"OUROBOROS_REDIS_URL sentinel address needs host:port, got {addr!r}")
    return host, int(port)


def _connection_kwargs(*, retry: Any, connect_timeout: float, socket_timeout: float) -> dict[str, Any]:
    return {
        "decode_responses": True,
        "socket_connect_timeout": connect_timeout,
        "socket_timeout": socket_timeout,
        "health_check_interval": HEALTH_CHECK_SEC,
        "retry": retry,
    }


def _sentinel_connection_kwargs(
    parsed: dict[str, Any], *, retry: Any, connect_timeout: float, socket_timeout: float
) -> dict[str, Any]:
    connection_kwargs = _connection_kwargs(retry=retry, connect_timeout=connect_timeout, socket_timeout=socket_timeout)
    connection_kwargs["ssl"] = bool(parsed["ssl"])
    connection_kwargs.update(parsed["kwargs"])
    if parsed["username"] is not None or parsed["password"] is not None:
        connection_kwargs["username"] = parsed["username"]
        connection_kwargs["password"] = parsed["password"]
    return connection_kwargs


def _sentinel_kwargs(parsed: dict[str, Any], connection_kwargs: dict[str, Any]) -> dict[str, Any]:
    sentinel_kwargs = {k: v for k, v in connection_kwargs.items() if k.startswith("socket_")}
    sentinel_kwargs["ssl"] = bool(parsed["ssl"])
    if "username" in connection_kwargs:
        sentinel_kwargs["username"] = connection_kwargs["username"]
        sentinel_kwargs["password"] = connection_kwargs["password"]
    return sentinel_kwargs


def _configured_url() -> str:
    url = str(load_settings().OUROBOROS_REDIS_URL or "").strip()
    if not url:
        raise RuntimeError(
            "OUROBOROS_BROKER_BACKEND=redis needs OUROBOROS_REDIS_URL, which is empty. "
            "Set the URL or put OUROBOROS_BROKER_BACKEND back to 'inprocess': composing the "
            "in-process backend instead would give every instance its own queue."
        )
    return url


def _is_sentinel(url: str) -> bool:
    return (urlsplit(url).scheme or "").lower() in SENTINEL_SCHEMES


def redis_client(*, retries: int, connect_timeout: float, socket_timeout: float) -> redis.Redis:
    url = _configured_url()
    retry = Retry(NoBackoff(), retries)
    if _is_sentinel(url):
        parsed = parse_sentinel_url(url)
        connection_kwargs = _sentinel_connection_kwargs(
            parsed, retry=retry, connect_timeout=connect_timeout, socket_timeout=socket_timeout
        )
        sentinel = Sentinel(
            sentinels=parsed["sentinels"],
            sentinel_kwargs=_sentinel_kwargs(parsed, connection_kwargs),
            **connection_kwargs,
        )
        return sentinel.master_for(parsed["service_name"], db=parsed["db"])
    return redis.Redis.from_url(
        url, **_connection_kwargs(retry=retry, connect_timeout=connect_timeout, socket_timeout=socket_timeout)
    )


def async_redis_client(*, retries: int, connect_timeout: float, socket_timeout: float) -> redis.asyncio.Redis:
    url = _configured_url()
    retry = AsyncRetry(NoBackoff(), retries)
    if _is_sentinel(url):
        parsed = parse_sentinel_url(url)
        connection_kwargs = _sentinel_connection_kwargs(
            parsed, retry=retry, connect_timeout=connect_timeout, socket_timeout=socket_timeout
        )
        sentinel = AsyncSentinel(
            sentinels=parsed["sentinels"],
            sentinel_kwargs=_sentinel_kwargs(parsed, connection_kwargs),
            **connection_kwargs,
        )
        return sentinel.master_for(parsed["service_name"], db=parsed["db"])
    return redis.asyncio.Redis.from_url(
        url, **_connection_kwargs(retry=retry, connect_timeout=connect_timeout, socket_timeout=socket_timeout)
    )


__all__ = [
    "BROKER_CONNECT_TIMEOUT_SEC",
    "BROKER_SOCKET_TIMEOUT_SEC",
    "HEALTH_CHECK_SEC",
    "SENTINEL_SCHEMES",
    "async_redis_client",
    "key_prefix",
    "parse_sentinel_url",
    "redis_client",
]
