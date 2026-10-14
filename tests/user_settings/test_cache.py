from __future__ import annotations

import asyncio

import pytest
import redis

from config_registry.user_settings.infra.cache import settings_cache_key
from config_registry.user_settings.reader import UserSettings
from infra.cache.inmemory import InMemoryCache
from infra.cache.null import AsyncNullCache, NullCache
from infra.cache.redis_cache import AsyncRedisCache, RedisCache
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.storage.settings.store import load_settings
from tests.helpers.redis_double import async_client_onto, client_onto, fake_server
from tests.helpers.user_settings import NoUserSettingsWrites, StubAsyncUserSettingsStore, StubUserSettingsStore

OWNER = "alice"
OTHER_OWNER = "bob"
TTL_SEC = 300
DECLARATION = (
    "OUROBOROS_USER_ENABLED_MCP_IDS",
    "OUROBOROS_EFFORT_TASK",
    "OUROBOROS_LOCALE",
    "OUROBOROS_MEMORY_DISTILL",
    "OUROBOROS_MODEL",
    "OUROBOROS_MODEL_FALLBACK",
    "OUROBOROS_MODEL_LIGHT",
    "OUROBOROS_TIMEZONE",
    "OUROBOROS_USER_ENABLED_SKILL_IDS",
)


@pytest.fixture(params=["inmemory", "redis"])
def cache(request):
    if request.param == "inmemory":
        return InMemoryCache(ttl_sec=TTL_SEC)
    return RedisCache(client_onto(fake_server()), ttl_sec=TTL_SEC)


def _scope(owner_id: str = OWNER, agent_id: str = "main") -> OwnerScope:
    return OwnerScope(owner_id=owner_id, agent_id=agent_id, surface_id=SurfaceId.BROWSER, chat_id="c1")


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


def test_a_key_is_read_from_the_store_once_and_answered_from_its_entry_after(cache):
    store = StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Tokyo"})
    settings = UserSettings(
        load_settings(), store, StubAsyncUserSettingsStore(), NoUserSettingsWrites(), cache, AsyncNullCache()
    )

    for _ in range(4):
        assert settings.OUROBOROS_TIMEZONE(_scope()) == "Asia/Tokyo"

    assert store.reads == 1


def test_a_key_with_no_override_is_read_once_and_resolves_the_instance_value(cache):
    store = StubUserSettingsStore({})
    settings = UserSettings(
        load_settings(), store, StubAsyncUserSettingsStore(), NoUserSettingsWrites(), cache, AsyncNullCache()
    )

    for _ in range(4):
        assert settings.OUROBOROS_TIMEZONE(_scope()) == load_settings().OUROBOROS_TIMEZONE

    assert store.reads == 1, "an absent override is a cached answer, not a miss on every call"


def test_each_key_holds_its_own_entry_and_answers_for_no_other(cache):
    store = StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Tokyo", "OUROBOROS_MODEL": "mine"})
    settings = UserSettings(
        load_settings(), store, StubAsyncUserSettingsStore(), NoUserSettingsWrites(), cache, AsyncNullCache()
    )

    assert settings.OUROBOROS_TIMEZONE(_scope()) == "Asia/Tokyo"
    assert settings.OUROBOROS_MODEL(_scope()) == "mine"
    assert settings.OUROBOROS_LOCALE(_scope()) == load_settings().OUROBOROS_LOCALE

    assert store.reads == 3


def test_a_cold_scope_costs_one_read_per_key_of_the_declaration(cache):
    """The store answers one key at a time, so the declaration's size is the cold cost."""
    store = StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Tokyo"})
    settings = UserSettings(
        load_settings(), store, StubAsyncUserSettingsStore(), NoUserSettingsWrites(), cache, AsyncNullCache()
    )

    for key in DECLARATION:
        getattr(settings, key)(_scope())
    for key in DECLARATION:
        getattr(settings, key)(_scope())

    assert store.reads == len(DECLARATION), "the second pass is served entirely from the cache"


def test_two_agents_of_one_owner_hold_independent_entries(cache):
    store = StubUserSettingsStore(by_pair={(OWNER, "main"): {"OUROBOROS_MODEL": "mine"}})
    settings = UserSettings(
        load_settings(), store, StubAsyncUserSettingsStore(), NoUserSettingsWrites(), cache, AsyncNullCache()
    )

    assert settings.OUROBOROS_MODEL(_scope(agent_id="main")) == "mine"
    assert settings.OUROBOROS_MODEL(_scope(agent_id="sub-1")) == load_settings().OUROBOROS_MODEL


def test_two_owners_on_one_agent_id_hold_independent_entries(cache):
    store = StubUserSettingsStore(
        by_pair={(OWNER, "main"): {"OUROBOROS_MODEL": "mine"}, (OTHER_OWNER, "main"): {"OUROBOROS_MODEL": "theirs"}}
    )
    settings = UserSettings(
        load_settings(), store, StubAsyncUserSettingsStore(), NoUserSettingsWrites(), cache, AsyncNullCache()
    )

    assert settings.OUROBOROS_MODEL(_scope(OWNER)) == "mine"
    assert settings.OUROBOROS_MODEL(_scope(OTHER_OWNER)) == "theirs"


def test_a_cache_does_not_change_what_resolves(cache):
    overrides = {"OUROBOROS_TIMEZONE": "Asia/Tokyo", "OUROBOROS_MEMORY_DISTILL": "false", "OUROBOROS_LOCALE": ""}
    cached = UserSettings(
        load_settings(),
        StubUserSettingsStore(overrides),
        StubAsyncUserSettingsStore(),
        NoUserSettingsWrites(),
        cache,
        AsyncNullCache(),
    )
    uncached = UserSettings(
        load_settings(),
        StubUserSettingsStore(overrides),
        StubAsyncUserSettingsStore(),
        NoUserSettingsWrites(),
        NullCache(),
        AsyncNullCache(),
    )

    for key in ("OUROBOROS_TIMEZONE", "OUROBOROS_MEMORY_DISTILL", "OUROBOROS_LOCALE", "OUROBOROS_MODEL"):
        assert getattr(cached, key)(_scope()) == getattr(uncached, key)(_scope())


def test_an_entry_is_not_served_past_its_window():
    store = StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Tokyo"})
    settings = UserSettings(
        load_settings(),
        store,
        StubAsyncUserSettingsStore(),
        NoUserSettingsWrites(),
        InMemoryCache(ttl_sec=0),
        AsyncNullCache(),
    )

    settings.OUROBOROS_TIMEZONE(_scope())
    settings.OUROBOROS_TIMEZONE(_scope())

    assert store.reads == 2


def test_a_key_is_stored_under_its_own_entry_with_the_configured_expiry():
    client = client_onto(fake_server())
    settings = UserSettings(
        load_settings(),
        StubUserSettingsStore({}),
        StubAsyncUserSettingsStore(),
        NoUserSettingsWrites(),
        RedisCache(client, ttl_sec=TTL_SEC),
        AsyncNullCache(),
    )

    settings.OUROBOROS_TIMEZONE(_scope())

    entry = f"ouroboros:{settings_cache_key(OWNER, 'main', 'OUROBOROS_TIMEZONE')}"
    assert client.get(entry) == '{"value": null}', "an absence is stored as an entry, not left as a miss"
    assert 0 < client.ttl(entry) <= TTL_SEC


def test_a_shared_cache_reaches_a_reader_in_another_process():
    """The reader is synchronous and the writer is not; they meet on the one keyspace."""
    server = fake_server()
    store = StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Tokyo"})
    reader = UserSettings(
        load_settings(),
        store,
        StubAsyncUserSettingsStore(),
        NoUserSettingsWrites(),
        RedisCache(client_onto(server), ttl_sec=TTL_SEC),
        AsyncNullCache(),
    )
    writer = AsyncRedisCache(async_client_onto(server), ttl_sec=TTL_SEC)

    assert reader.OUROBOROS_TIMEZONE(_scope()) == "Asia/Tokyo"
    assert store.reads == 1

    asyncio.get_event_loop().run_until_complete(
        writer.delete_value(settings_cache_key(OWNER, "main", "OUROBOROS_TIMEZONE"))
    )

    reader.OUROBOROS_TIMEZONE(_scope())
    assert store.reads == 2


def test_a_drop_reaches_the_key_it_names_and_leaves_the_others_alone():
    server = fake_server()
    store = StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Tokyo", "OUROBOROS_MODEL": "mine"})
    reader = UserSettings(
        load_settings(),
        store,
        StubAsyncUserSettingsStore(),
        NoUserSettingsWrites(),
        RedisCache(client_onto(server), ttl_sec=TTL_SEC),
        AsyncNullCache(),
    )
    writer = AsyncRedisCache(async_client_onto(server), ttl_sec=TTL_SEC)
    reader.OUROBOROS_TIMEZONE(_scope())
    reader.OUROBOROS_MODEL(_scope())

    asyncio.get_event_loop().run_until_complete(
        writer.delete_value(settings_cache_key(OWNER, "main", "OUROBOROS_MODEL"))
    )

    reader.OUROBOROS_TIMEZONE(_scope())
    reader.OUROBOROS_MODEL(_scope())
    assert store.reads == 3


def test_a_failing_client_degrades_the_read_to_the_store():
    store = StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Tokyo"})
    settings = UserSettings(
        load_settings(),
        store,
        StubAsyncUserSettingsStore(),
        NoUserSettingsWrites(),
        RedisCache(_FailingClient(), ttl_sec=TTL_SEC),
        AsyncNullCache(),
    )

    assert settings.OUROBOROS_TIMEZONE(_scope()) == "Asia/Tokyo"
    assert settings.OUROBOROS_TIMEZONE(_scope()) == "Asia/Tokyo"
    assert store.reads == 2


def test_an_unreachable_cache_is_asked_once_per_window_and_not_once_per_key():
    client = _FailingClient()
    store = StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Tokyo"})
    settings = UserSettings(
        load_settings(),
        store,
        StubAsyncUserSettingsStore(),
        NoUserSettingsWrites(),
        RedisCache(client, ttl_sec=TTL_SEC),
        AsyncNullCache(),
    )

    for _ in range(4):
        assert settings.OUROBOROS_TIMEZONE(_scope()) == "Asia/Tokyo"
        assert settings.OUROBOROS_MODEL(_scope()) == load_settings().OUROBOROS_MODEL

    assert client.calls == 1
    assert store.reads == 8, "every read still reaches the store, which is the source of truth"


def test_a_failing_invalidation_propagates_to_the_caller():
    writer = AsyncRedisCache(_FailingAsyncClient(), ttl_sec=TTL_SEC)

    with pytest.raises(redis.ConnectionError):
        asyncio.get_event_loop().run_until_complete(
            writer.delete_value(settings_cache_key(OWNER, "main", "OUROBOROS_MODEL"))
        )


def test_an_entry_of_another_shape_falls_through_to_the_store():
    client = client_onto(fake_server())
    client.set(f"ouroboros:{settings_cache_key(OWNER, 'main', 'OUROBOROS_TIMEZONE')}", '["not", "an", "entry"]')
    store = StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Tokyo"})
    settings = UserSettings(
        load_settings(),
        store,
        StubAsyncUserSettingsStore(),
        NoUserSettingsWrites(),
        RedisCache(client, ttl_sec=TTL_SEC),
        AsyncNullCache(),
    )

    assert settings.OUROBOROS_TIMEZONE(_scope()) == "Asia/Tokyo"
    assert store.reads == 1
