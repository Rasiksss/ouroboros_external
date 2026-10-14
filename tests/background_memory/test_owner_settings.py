"""The four owner-settings keys a memory handler reads, through the reader's async face."""

from __future__ import annotations

import asyncio

import pytest

from config_registry.user_settings.infra.cache import settings_cache_key
from config_registry.user_settings.infra.repositories.repo import (
    SqlAsyncUserSettingsStore,
    SqlUserSettingsRepo,
    SqlUserSettingsStore,
)
from config_registry.user_settings.reader import UserSettings
from infra.async_engine import async_engine_factory
from infra.cache.inmemory import AsyncInMemoryCache, InMemoryCache
from infra.cache.null import NullCache
from ouroboros.storage.settings.store import load_settings
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.user_settings import NoUserSettingsWrites

SCOPE = TEST_SCOPE


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def async_cache() -> AsyncInMemoryCache:
    return AsyncInMemoryCache(InMemoryCache(ttl_sec=300))


@pytest.fixture
def settings(runtime_db_url: str, async_cache: AsyncInMemoryCache) -> UserSettings:
    """The reader as a memory handler holds it: the real async store, its own async cache."""
    async_engine_factory.initialize(runtime_db_url)
    return UserSettings(
        load_settings(),
        SqlUserSettingsStore(),
        SqlAsyncUserSettingsStore(),
        NoUserSettingsWrites(),
        NullCache(),
        async_cache,
    )


def _write(key: str, value: str, *, owner_id: str = SCOPE.owner_id) -> None:
    repo = SqlUserSettingsRepo(async_engine_factory.engine)
    _run(repo.set_override(owner_id, SCOPE.agent_id, key, value))


def test_an_owner_with_no_override_reads_the_instances_own_values(settings) -> None:
    instance = load_settings()

    assert _run(settings.async_OUROBOROS_MODEL_FALLBACK(SCOPE)) == instance.OUROBOROS_MODEL_FALLBACK
    assert _run(settings.async_OUROBOROS_MEMORY_DISTILL(SCOPE)) == bool(instance.OUROBOROS_MEMORY_DISTILL)


def test_the_read_reflects_a_value_written_through_the_owner_settings_surface(settings) -> None:
    """The surface's own writer, so the two cannot agree on a key and disagree on the row."""
    _write("OUROBOROS_MODEL_LIGHT", "chosen-light")
    _write("OUROBOROS_MODEL", "chosen-main")
    _write("OUROBOROS_MODEL_FALLBACK", "chosen-fallback")
    _write("OUROBOROS_MEMORY_DISTILL", "false")

    resolved = (
        _run(settings.async_OUROBOROS_MODEL_LIGHT(SCOPE)),
        _run(settings.async_OUROBOROS_MODEL(SCOPE)),
        _run(settings.async_OUROBOROS_MODEL_FALLBACK(SCOPE)),
    )

    assert resolved == ("chosen-light", "chosen-main", "chosen-fallback")
    assert _run(settings.async_OUROBOROS_MEMORY_DISTILL(SCOPE)) is False


def test_a_second_owners_override_is_not_this_owners(settings) -> None:
    _write("OUROBOROS_MODEL", "theirs", owner_id="someone-else")

    assert _run(settings.async_OUROBOROS_MODEL(SCOPE)) != "theirs"


def test_the_answer_is_served_from_the_async_cache_until_its_entry_is_dropped(settings, async_cache) -> None:
    """The entry the settings surface invalidates: both faces meet on `settings_cache_key`."""
    assert _run(settings.async_OUROBOROS_MODEL_LIGHT(SCOPE)) != "second-answer"

    _write("OUROBOROS_MODEL_LIGHT", "second-answer")

    assert _run(settings.async_OUROBOROS_MODEL_LIGHT(SCOPE)) != "second-answer"
    _run(async_cache.delete_value(settings_cache_key(SCOPE.owner_id, SCOPE.agent_id, "OUROBOROS_MODEL_LIGHT")))
    assert _run(settings.async_OUROBOROS_MODEL_LIGHT(SCOPE)) == "second-answer"


def test_an_unreadable_boolean_override_is_refused_rather_than_guessed(settings) -> None:
    _write("OUROBOROS_MEMORY_DISTILL", "maybe")

    with pytest.raises(ValueError):
        _run(settings.async_OUROBOROS_MEMORY_DISTILL(SCOPE))
