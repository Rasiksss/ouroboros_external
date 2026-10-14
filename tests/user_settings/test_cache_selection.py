from __future__ import annotations

import ast
import asyncio
import pathlib
import subprocess
import sys

import pytest

from config_registry.user_settings.infra.cache import settings_cache_key
from infra.cache import (
    CONNECT_TIMEOUT_SEC,
    SOCKET_TIMEOUT_SEC,
    CacheFactory,
    cache_factory,
    refuse_a_process_local_settings_cache,
)
from infra.cache.inmemory import AsyncInMemoryCache, InMemoryCache
from infra.cache.null import AsyncNullCache, NullCache
from new_core.identity import OwnerScope, SurfaceId
from tests.helpers.redis_double import client_onto, fake_server
from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TTL_SEC = 300
_TIER_ROOTS = [
    REPO_ROOT / "multiuser_config" / "bootstrap.py",
    REPO_ROOT / "multiuser_config" / "config_registry" / "app.py",
    REPO_ROOT / "multiuser_config" / "transport" / "browser" / "app.py",
]


@pytest.fixture(autouse=True)
def _fresh_holders():
    """Every test starts from the backend a root would compose from the default settings."""
    cache_factory.initialize("none", TTL_SEC)


def test_a_factory_no_root_initialized_fails_loudly_rather_than_composing_a_default():
    """The surfaces are built at import and the cache at start-up; between them it must not guess."""
    uninitialized = CacheFactory()

    for reach in (uninitialized.cache, uninitialized.async_cache):
        with pytest.raises(RuntimeError, match="not initialized"):
            reach()


def test_the_default_composes_no_cache_at_all():
    cache_factory.initialize("none", TTL_SEC)

    assert isinstance(cache_factory.cache(), NullCache)
    assert isinstance(cache_factory.async_cache(), AsyncNullCache)


def test_with_no_cache_every_accessor_still_reads_the_store():
    cache_factory.initialize("none", TTL_SEC)
    store = StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Tokyo"})
    settings = _settings_over(store)

    settings.OUROBOROS_TIMEZONE(_scope())
    settings.OUROBOROS_TIMEZONE(_scope())

    assert store.reads == 2


def test_the_in_process_backend_is_composed_when_it_is_named():
    cache_factory.initialize("inmemory", TTL_SEC)

    assert isinstance(cache_factory.cache(), InMemoryCache)
    assert isinstance(cache_factory.async_cache(), AsyncInMemoryCache)


def test_a_backend_that_is_not_one_of_the_three_is_refused():
    with pytest.raises(RuntimeError, match="OUROBOROS_SETTINGS_CACHE_BACKEND"):
        cache_factory.initialize("memcached", TTL_SEC)


def test_the_reader_and_the_writer_are_handed_one_set_of_in_process_entries():
    cache_factory.initialize("inmemory", TTL_SEC)
    reader, writer = cache_factory.cache(), cache_factory.async_cache()

    reader.set_value("k", {"value": "one"})

    assert asyncio.get_event_loop().run_until_complete(writer.get_value("k")) == {"value": "one"}
    assert cache_factory.cache() is reader
    assert cache_factory.async_cache() is writer


def test_a_component_tier_refuses_the_in_process_cache(monkeypatch):
    monkeypatch.setenv("OUROBOROS_SETTINGS_CACHE_BACKEND", "inmemory")

    with pytest.raises(RuntimeError, match="component tier"):
        refuse_a_process_local_settings_cache("agent-registry")


@pytest.mark.parametrize("backend", ["none", "redis"])
def test_a_component_tier_accepts_every_other_backend(monkeypatch, backend):
    monkeypatch.setenv("OUROBOROS_SETTINGS_CACHE_BACKEND", backend)

    refuse_a_process_local_settings_cache("agent-registry")


def test_the_shared_backend_that_cannot_be_composed_stops_start_up(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "")

    with pytest.raises(RuntimeError, match="OUROBOROS_SETTINGS_CACHE_BACKEND"):
        cache_factory.initialize("redis", TTL_SEC)


def test_no_refusal_of_this_change_names_a_connection_url(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "")
    with pytest.raises(RuntimeError) as unbuildable:
        cache_factory.initialize("redis", TTL_SEC)

    monkeypatch.setenv("OUROBOROS_SETTINGS_CACHE_BACKEND", "inmemory")
    with pytest.raises(RuntimeError) as in_a_tier:
        refuse_a_process_local_settings_cache("agent-registry")

    for message in (str(unbuildable.value), str(in_a_tier.value)):
        assert "://" not in message


def test_rebuilding_the_settings_object_leaves_no_cached_value_reachable(monkeypatch):
    from config_registry.user_settings import reader

    monkeypatch.setenv("OUROBOROS_SETTINGS_CACHE_BACKEND", "inmemory")

    reset_user_settings_for_tests(StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Tokyo"}), cache_factory.cache())
    assert reader.user_settings_factory.settings().OUROBOROS_TIMEZONE(_scope()) == "Asia/Tokyo"

    reset_user_settings_for_tests(StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Europe/Berlin"}))
    assert reader.user_settings_factory.settings().OUROBOROS_TIMEZONE(_scope()) == "Europe/Berlin"


def test_the_settings_object_composes_itself_over_the_shared_cache_and_sql_stores():
    from config_registry.user_settings import reader
    from config_registry.user_settings.infra.repositories.repo import SqlUserSettingsStore

    cache_factory.initialize("inmemory", TTL_SEC)
    reader.user_settings_factory.reset_for_tests(None)

    settings = reader.user_settings_factory.settings()

    assert settings._cache is cache_factory.cache()
    assert isinstance(settings._store, SqlUserSettingsStore)
    assert reader.user_settings_factory.settings() is settings


def test_the_settings_object_refuses_to_compose_before_the_cache_is(monkeypatch):
    from config_registry.user_settings import reader

    monkeypatch.setattr("config_registry.user_settings.reader.cache_factory", CacheFactory())
    reader.user_settings_factory.reset_for_tests(None)

    with pytest.raises(RuntimeError, match="not initialized"):
        reader.user_settings_factory.settings()


def test_a_process_that_selected_no_cache_never_imports_redis():
    code = (
        "import sys\n"
        "from config_registry.user_settings.reader import user_settings_factory\n"
        "from infra.cache import cache_factory\n"
        "cache_factory.initialize('none', 300)\n"
        "user_settings_factory.settings()\n"
        "leaked = [m for m in sys.modules if m == 'redis' or m.startswith('redis.')]\n"
        "assert not leaked, leaked\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(REPO_ROOT))

    assert proc.returncode == 0, proc.stderr


def test_the_selector_names_redis_only_inside_the_branch_that_selects_it():
    tree = ast.parse((REPO_ROOT / "infra" / "cache" / "__init__.py").read_text(encoding="utf-8"))
    top_level = [node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)]

    named = {alias.name for node in top_level if isinstance(node, ast.Import) for alias in node.names}
    named |= {node.module or "" for node in top_level if isinstance(node, ast.ImportFrom)}

    assert not any(name == "redis" or name.startswith("redis") for name in named), named
    assert not any(name.endswith(("redis_cache", "redis_client")) for name in named), named


def test_the_shared_cache_clients_take_a_sub_second_budget_and_no_retry(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "redis://localhost:6379/0")
    cache_factory.initialize("redis", TTL_SEC)

    for cache in (cache_factory.cache(), cache_factory.async_cache()):
        composed = cache._client.connection_pool.connection_kwargs
        assert composed["socket_connect_timeout"] == CONNECT_TIMEOUT_SEC < 1.0
        assert composed["socket_timeout"] == SOCKET_TIMEOUT_SEC < 1.0
        assert composed["retry"]._retries == 0


def test_the_cache_keyspace_cannot_collide_with_the_brokers(monkeypatch):
    from infra.cache.redis_cache import RedisCache
    from ouroboros.app.broker_composition import stream_key

    monkeypatch.setenv("OUROBOROS_REDIS_PREFIX", "fleet-a")
    client = client_onto(fake_server())
    RedisCache(client, ttl_sec=300).set_value(settings_cache_key("alice", "main", "OUROBOROS_MODEL"), {})

    written = list(client.keys("*"))
    assert written == ["fleet-a:user-settings:alice:main:OUROBOROS_MODEL"]
    assert not any(
        written[0].startswith(stream_key(suffix)) for suffix in ("inbound", "outbound", "agent-events", "memory-events")
    )


def _settings_over(store):
    from config_registry.user_settings.reader import UserSettings
    from ouroboros.storage.settings.store import load_settings
    from tests.helpers.user_settings import NoUserSettingsWrites, StubAsyncUserSettingsStore

    return UserSettings(
        load_settings(),
        store,
        StubAsyncUserSettingsStore(),
        NoUserSettingsWrites(),
        cache_factory.cache(),
        cache_factory.async_cache(),
    )


def _scope() -> OwnerScope:
    return OwnerScope(owner_id="alice", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="c1")
