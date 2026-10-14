"""The cache backends behind `ICache` and `IAsyncCache`, apart from any caller's use of them."""

from __future__ import annotations

import asyncio

import pytest
import redis

from infra.cache.inmemory import AsyncInMemoryCache, InMemoryCache
from infra.cache.null import AsyncNullCache, NullCache
from infra.cache.redis_cache import FAILURE_WINDOW_SEC, AsyncRedisCache, RedisCache
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.redis_double import async_client_onto, client_onto, fake_server

TTL_SEC = 300


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FailingClient:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        raise redis.ConnectionError("redis is down")

    def set(self, *_args, **_kwargs):
        self.calls += 1
        raise redis.ConnectionError("redis is down")

    def delete(self, *_args, **_kwargs):
        self.calls += 1
        raise redis.ConnectionError("redis is down")


class _FailingAsyncClient:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, *_args, **_kwargs):
        self.calls += 1
        raise redis.ConnectionError("redis is down")

    async def set(self, *_args, **_kwargs):
        self.calls += 1
        raise redis.ConnectionError("redis is down")

    async def delete(self, *_args, **_kwargs):
        self.calls += 1
        raise redis.ConnectionError("redis is down")


@pytest.fixture(params=["inmemory", "redis"])
def cache(request):
    if request.param == "inmemory":
        return InMemoryCache(ttl_sec=TTL_SEC)
    return RedisCache(client_onto(fake_server()), ttl_sec=TTL_SEC)


@pytest.fixture(params=["inmemory", "redis"])
def async_cache(request):
    if request.param == "inmemory":
        return AsyncInMemoryCache(InMemoryCache(ttl_sec=TTL_SEC))
    return AsyncRedisCache(async_client_onto(fake_server()), ttl_sec=TTL_SEC)


basic_kv_cases = {
    "absent key is none": {"key": "nothing:here", "set_before": None, "expected": None},
    "stored value is read back": {"key": "k", "set_before": {"a": "1"}, "expected": {"a": "1"}},
    "empty object is a value not an absence": {"key": "k", "set_before": {}, "expected": {}},
}


@parametrize_from_dict(basic_kv_cases)
def test_cache_get_set_delete(cache, key, set_before, expected):
    if set_before is not None:
        cache.set_value(key, set_before)
    assert cache.get_value(key) == expected


def test_a_deleted_key_is_absent_again(cache):
    cache.set_value("k", {"a": "1"})
    cache.delete_value("k")
    assert cache.get_value("k") is None


def test_deleting_the_key_that_is_not_there_is_not_an_error(cache):
    cache.delete_value("never-written")


@parametrize_from_dict(
    {
        "two keys do not answer for each other": {
            "entries": [("one", {"a": "1"}), ("two", {"a": "2"})],
            "expected": ({"a": "1"}, {"a": "2"}),
        },
    }
)
def test_two_keys_do_not_answer_for_each_other(cache, entries, expected):
    for k, v in entries:
        cache.set_value(k, v)
    assert (cache.get_value(entries[0][0]), cache.get_value(entries[1][0])) == expected


def test_an_entry_is_not_served_past_the_window_it_was_given():
    cache = InMemoryCache(ttl_sec=300)
    cache.set_value("k", {"a": "1"}, ttl=0)
    assert cache.get_value("k") is None


def test_the_default_window_applies_when_the_caller_names_none():
    cache = InMemoryCache(ttl_sec=0)
    cache.set_value("k", {"a": "1"})
    assert cache.get_value("k") is None


def test_the_redis_key_carries_the_deployment_prefix(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REDIS_PREFIX", "fleet-a")
    client = client_onto(fake_server())
    RedisCache(client, ttl_sec=TTL_SEC).set_value("user-settings:alice:main", {"a": "1"})
    assert client.get("fleet-a:user-settings:alice:main") == '{"a": "1"}'


def test_the_redis_entry_carries_the_configured_expiry():
    client = client_onto(fake_server())
    RedisCache(client, ttl_sec=TTL_SEC).set_value("k", {"a": "1"})
    assert 0 < client.ttl("ouroboros:k") <= TTL_SEC


def test_a_value_that_is_not_json_reads_as_an_absence():
    client = client_onto(fake_server())
    client.set("ouroboros:k", "not json")
    assert RedisCache(client, ttl_sec=TTL_SEC).get_value("k") is None


unreachable_cases = {
    "read is absence not failure": {"method": "get_value", "expected": None},
    "write is swallowed": {"method": "set_value", "expected": None},
}


@parametrize_from_dict(unreachable_cases)
def test_unreachable_client(method, expected):
    cache = RedisCache(_FailingClient(), ttl_sec=TTL_SEC)
    if method == "get_value":
        assert cache.get_value("k") == expected
    else:
        cache.set_value("k", {"a": "1"})


def test_an_unreachable_client_is_left_alone_for_the_window():
    client = _FailingClient()
    cache = RedisCache(client, ttl_sec=TTL_SEC)
    for _ in range(5):
        cache.get_value("k")
        cache.set_value("k", {"a": "1"})
    assert client.calls == 1
    assert FAILURE_WINDOW_SEC > 0


def test_an_unreachable_delete_reaches_the_caller():
    with pytest.raises(redis.ConnectionError):
        RedisCache(_FailingClient(), ttl_sec=TTL_SEC).delete_value("k")


def test_a_delete_is_attempted_even_while_the_client_is_held_off():
    client = _FailingClient()
    cache = RedisCache(client, ttl_sec=TTL_SEC)
    cache.get_value("k")
    with pytest.raises(redis.ConnectionError):
        cache.delete_value("k")
    assert client.calls == 2, "a write must fail rather than report a drop that never happened"


def test_the_null_cache_holds_nothing():
    cache = NullCache()
    cache.set_value("k", {"a": "1"})
    cache.delete_value("k")
    assert cache.get_value("k") is None


# --- async face ---


@parametrize_from_dict(basic_kv_cases)
def test_cache_get_set_delete_async(async_cache, key, set_before, expected):
    if set_before is not None:
        _run(async_cache.set_value(key, set_before))
    assert _run(async_cache.get_value(key)) == expected


def test_a_deleted_key_is_absent_again_async(async_cache):
    _run(async_cache.set_value("k", {"a": "1"}))
    _run(async_cache.delete_value("k"))
    assert _run(async_cache.get_value("k")) is None


def test_the_two_in_process_faces_share_one_set_of_entries():
    entries = InMemoryCache(ttl_sec=TTL_SEC)
    reader, writer = entries, AsyncInMemoryCache(entries)
    reader.set_value("k", {"a": "1"})
    assert _run(writer.get_value("k")) == {"a": "1"}
    _run(writer.delete_value("k"))
    assert reader.get_value("k") is None


def test_the_async_redis_key_carries_the_deployment_prefix(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REDIS_PREFIX", "fleet-a")
    server = fake_server()
    _run(
        AsyncRedisCache(async_client_onto(server), ttl_sec=TTL_SEC).set_value("user-settings:alice:main:K", {"a": "1"})
    )
    assert client_onto(server).get("fleet-a:user-settings:alice:main:K") == '{"a": "1"}'


def test_the_async_entry_carries_the_configured_expiry():
    server = fake_server()
    _run(AsyncRedisCache(async_client_onto(server), ttl_sec=TTL_SEC).set_value("k", {"a": "1"}))
    assert 0 < client_onto(server).ttl("ouroboros:k") <= TTL_SEC


def test_an_unreachable_async_read_is_an_absence_rather_than_a_failure():
    assert _run(AsyncRedisCache(_FailingAsyncClient(), ttl_sec=TTL_SEC).get_value("k")) is None


def test_an_unreachable_async_client_is_left_alone_for_the_window():
    client = _FailingAsyncClient()
    cache = AsyncRedisCache(client, ttl_sec=TTL_SEC)
    for _ in range(5):
        _run(cache.get_value("k"))
        _run(cache.set_value("k", {"a": "1"}))
    assert client.calls == 1


def test_an_unreachable_async_delete_reaches_the_caller():
    client = _FailingAsyncClient()
    cache = AsyncRedisCache(client, ttl_sec=TTL_SEC)
    _run(cache.get_value("k"))
    with pytest.raises(redis.ConnectionError):
        _run(cache.delete_value("k"))
    assert client.calls == 2, "a write must fail rather than report a drop that never happened"


def test_the_async_null_cache_holds_nothing():
    cache = AsyncNullCache()
    _run(cache.set_value("k", {"a": "1"}))
    _run(cache.delete_value("k"))
    assert _run(cache.get_value("k")) is None
