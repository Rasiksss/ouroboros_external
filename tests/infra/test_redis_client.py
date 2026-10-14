from __future__ import annotations

import pytest

from infra.redis_client import async_redis_client, redis_client

PASSWORD = "hunter2"


def _client(**overrides):
    kwargs = {"retries": 0, "connect_timeout": 0.25, "socket_timeout": 0.25}
    kwargs.update(overrides)
    return redis_client(**kwargs)


def _async_client(**overrides):
    kwargs = {"retries": 0, "connect_timeout": 0.25, "socket_timeout": 0.25}
    kwargs.update(overrides)
    return async_redis_client(**kwargs)


def test_a_plain_url_carries_the_callers_budget_and_the_shared_options(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "redis://localhost:6379/0")

    composed = _client(retries=2, connect_timeout=0.3, socket_timeout=0.4).connection_pool.connection_kwargs

    assert composed["socket_connect_timeout"] == 0.3
    assert composed["socket_timeout"] == 0.4
    assert composed["retry"]._retries == 2
    assert composed["decode_responses"] is True
    assert composed["health_check_interval"], "a connection the server reaped must not surface as an error"


@pytest.mark.parametrize("scheme", ["sentinel", "redis-sentinel", "rediss-sentinel"])
def test_every_sentinel_scheme_composes_a_failover_managed_client(monkeypatch, scheme) -> None:
    monkeypatch.setenv("OUROBOROS_REDIS_URL", f"{scheme}://sentinel-a:26379?service_name=mymaster&db=2")

    pool = _client(connect_timeout=0.3, socket_timeout=0.4).connection_pool

    assert type(pool).__name__ == "SentinelConnectionPool"
    assert pool.service_name == "mymaster"
    assert pool.connection_kwargs["db"] == 2
    assert pool.connection_kwargs["socket_connect_timeout"] == 0.3
    assert pool.connection_kwargs["socket_timeout"] == 0.4


def test_a_tls_sentinel_scheme_uses_the_ssl_connection_class(monkeypatch) -> None:
    from redis.sentinel import SentinelManagedSSLConnection

    monkeypatch.setenv("OUROBOROS_REDIS_URL", "rediss-sentinel://sentinel-a:26379?service_name=mymaster")

    assert _client().connection_pool.connection_class is SentinelManagedSSLConnection


def test_a_sentinel_host_without_a_port_is_refused(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "sentinel://sentinel-a?service_name=mymaster")

    with pytest.raises(RuntimeError, match="host:port"):
        _client()


@pytest.mark.parametrize(
    "url",
    [
        f"sentinel://user:{PASSWORD}@sentinel-a?service_name=mymaster",
        f"sentinel://user:{PASSWORD}@sentinel-a:26379",
        "",
    ],
)
def test_no_refusal_repeats_the_configured_url_or_its_credentials(monkeypatch, url) -> None:
    monkeypatch.setenv("OUROBOROS_REDIS_URL", url)

    with pytest.raises(RuntimeError) as refusal:
        _client()

    message = str(refusal.value)
    assert PASSWORD not in message
    assert not url or url not in message


def test_the_asyncio_client_carries_the_same_budget_and_options(monkeypatch) -> None:
    """One description of the connection, two transports — the drift this prevents is silent."""
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "redis://localhost:6379/0")

    composed = _async_client(retries=2, connect_timeout=0.3, socket_timeout=0.4).connection_pool.connection_kwargs
    sync = _client(retries=2, connect_timeout=0.3, socket_timeout=0.4).connection_pool.connection_kwargs

    assert composed["socket_connect_timeout"] == sync["socket_connect_timeout"] == 0.3
    assert composed["socket_timeout"] == sync["socket_timeout"] == 0.4
    assert composed["retry"]._retries == sync["retry"]._retries == 2
    assert composed["decode_responses"] is True
    assert composed["health_check_interval"] == sync["health_check_interval"]


@pytest.mark.parametrize("scheme", ["sentinel", "redis-sentinel", "rediss-sentinel"])
def test_the_asyncio_client_reads_every_sentinel_scheme_the_same_way(monkeypatch, scheme) -> None:
    monkeypatch.setenv("OUROBOROS_REDIS_URL", f"{scheme}://sentinel-a:26379?service_name=mymaster&db=2")

    pool = _async_client(connect_timeout=0.3, socket_timeout=0.4).connection_pool

    assert type(pool).__name__ == "SentinelConnectionPool"
    assert pool.service_name == "mymaster"
    assert pool.connection_kwargs["db"] == 2
    assert pool.connection_kwargs["socket_connect_timeout"] == 0.3
    assert pool.connection_kwargs["socket_timeout"] == 0.4


def test_the_asyncio_client_refuses_the_same_urls(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_REDIS_URL", f"sentinel://user:{PASSWORD}@sentinel-a?service_name=mymaster")

    with pytest.raises(RuntimeError) as refusal:
        _async_client()

    assert PASSWORD not in str(refusal.value)
