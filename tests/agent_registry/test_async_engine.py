import asyncio

import pytest

from infra.async_engine import ASYNC_MAX_OVERFLOW, ASYNC_POOL_SIZE, AsyncEngineFactory, create_async_runtime_engine
from infra.async_failover import async_postgres_failover_creator

_URLS = [
    "postgresql://u:secret@node-a:5432/ouroboros",
    "postgresql://u:secret@node-b:5432/ouroboros",
    "postgresql://u:secret@node-c:5432/ouroboros",
]


class _FakeConnection:
    def __init__(self, host: str) -> None:
        self.host = host


def _connect_double(monkeypatch, healthy: set[str], attempts: list[str]):

    async def _connect(**kwargs):
        host = str(kwargs.get("host"))
        attempts.append(host)
        if host not in healthy:
            raise OSError(f"{host} is unreachable")
        return _FakeConnection(host)

    monkeypatch.setattr("psycopg.AsyncConnection.connect", staticmethod(_connect))


def test_failover_moves_past_an_unreachable_url(monkeypatch):
    attempts: list[str] = []
    _connect_double(monkeypatch, healthy={"node-b", "node-c"}, attempts=attempts)
    creator = async_postgres_failover_creator(_URLS)
    connection = asyncio.get_event_loop().run_until_complete(creator())
    assert connection.host == "node-b"
    assert attempts == ["node-a", "node-b"]


def test_later_connections_prefer_the_last_good_url(monkeypatch):
    attempts: list[str] = []
    _connect_double(monkeypatch, healthy={"node-c"}, attempts=attempts)
    creator = async_postgres_failover_creator(_URLS)

    async def _twice():
        first = await creator()
        attempts.clear()
        second = await creator()
        return (first, second)

    (first, second) = asyncio.get_event_loop().run_until_complete(_twice())
    assert (first.host, second.host) == ("node-c", "node-c")
    assert attempts == ["node-c"]


def test_every_url_down_raises_the_last_failure(monkeypatch):
    _connect_double(monkeypatch, healthy=set(), attempts=[])
    creator = async_postgres_failover_creator(_URLS)
    with pytest.raises(OSError):
        asyncio.get_event_loop().run_until_complete(creator())


def test_a_single_url_is_not_a_failover_set():
    with pytest.raises(ValueError):
        async_postgres_failover_creator(_URLS[:1])


def test_a_postgres_url_is_rewritten_to_the_async_driver():
    engine = create_async_runtime_engine(_URLS[0])
    assert engine.url.get_driver_name() == "psycopg"
    asyncio.get_event_loop().run_until_complete(engine.dispose())


def test_a_url_of_another_dialect_is_refused_without_naming_one():
    with pytest.raises(ValueError) as raised:
        create_async_runtime_engine("sqlite:////tmp/runtime.sqlite3")
    assert "sqlite" not in str(raised.value).lower()
    assert "OUROBOROS_RUNTIME_DB_URL" in str(raised.value)


def test_a_mixed_failover_set_is_refused_without_naming_a_url():
    with pytest.raises(ValueError) as raised:
        create_async_runtime_engine([_URLS[0], "mysql://u:secret@node-b/ouroboros"])
    assert "secret" not in str(raised.value)
    assert "node-b" not in str(raised.value)


def test_an_uncomposed_holder_raises_without_naming_a_url():
    factory = AsyncEngineFactory()
    with pytest.raises(RuntimeError) as raised:
        _ = factory.engine
    assert "initialize" in str(raised.value)
    assert "://" not in str(raised.value)


def test_the_holder_builds_nothing_until_the_engine_is_asked_for(monkeypatch):
    built: list[object] = []
    monkeypatch.setattr(
        "infra.async_engine.create_async_runtime_engine", lambda *args, **kwargs: built.append(args) or "engine"
    )
    factory = AsyncEngineFactory()
    factory.initialize(_URLS[0])
    assert built == []
    assert factory.engine == "engine"
    assert len(built) == 1
    assert factory.engine == "engine"
    assert len(built) == 1


def test_the_holder_refuses_to_swap_an_engine_it_already_built():
    factory = AsyncEngineFactory()
    factory.initialize(_URLS[0])
    first = factory.engine
    assert factory.engine is first
    with pytest.raises(RuntimeError):
        factory.initialize(_URLS[1])
    asyncio.get_event_loop().run_until_complete(factory.dispose())


def test_the_async_engine_marks_disconnects_the_way_the_store_does():
    engine = create_async_runtime_engine(_URLS[0])
    try:
        assert list(engine.dialect.dispatch.handle_error), "no disconnect handler on the async engine"
    finally:
        asyncio.get_event_loop().run_until_complete(engine.dispose())


def test_a_postgres_pool_is_sized_for_point_queries_not_left_to_the_default():
    engine = create_async_runtime_engine(_URLS[0])
    try:
        assert engine.pool.size() == ASYNC_POOL_SIZE
        assert engine.pool._max_overflow == ASYNC_MAX_OVERFLOW
    finally:
        asyncio.get_event_loop().run_until_complete(engine.dispose())
