from __future__ import annotations

import asyncio
import pathlib

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import config_registry.user_settings.api.http.endpoint as endpoint
from config_registry.user_settings.interfaces import UserSettingsStore
from config_registry.user_settings.reader import UserSettings
from infra.cache import cache_factory
from infra.cache.interface import IAsyncCache
from ouroboros.storage.settings.store import load_settings
from tests.helpers.agent_registry import FakeAgentRegistry
from tests.helpers.user_settings import NoUserSettingsWrites, StubAsyncUserSettingsStore
from tests.user_settings.test_endpoint import FakeSettingsRepo

OWNER_HEADER = "X-Ouroboros-Owner-Id"
MINE = "owner-a"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class StoreOverRows(UserSettingsStore):
    def __init__(self, rows: dict[tuple[str, str, str], str]) -> None:
        self._rows = rows
        self.reads = 0

    def get_setting_value(self, owner_id: str, agent_id: str, key: str) -> str | None:
        self.reads += 1
        return self._rows.get((owner_id, agent_id, key))


class FailingCache(IAsyncCache):
    async def get_value(self, key: str) -> None:
        return None

    async def set_value(self, key: str, value: object, ttl: int | None = None) -> None:
        return None

    async def delete_value(self, key: str) -> None:
        raise ConnectionError("the cache is unreachable")


@pytest.fixture(autouse=True)
def _in_process_cache():
    """The one process where the writer and the readers meet: both faces over one set of entries."""
    cache_factory.initialize("inmemory", 300)


@pytest.fixture
def registry() -> FakeAgentRegistry:
    fake = FakeAgentRegistry()
    asyncio.run(fake.get_or_create_default_agent(MINE))
    return fake


@pytest.fixture
def settings_repo() -> FakeSettingsRepo:
    return FakeSettingsRepo()


def _client(settings_repo, registry, *, cache=None) -> TestClient:
    app = FastAPI()
    app.include_router(endpoint.router)
    app.dependency_overrides[endpoint.get_registry] = lambda: registry
    app.dependency_overrides[endpoint.get_settings_repo] = lambda: settings_repo
    if cache is not None:
        app.dependency_overrides[endpoint.get_settings_cache] = lambda: cache
    return TestClient(app)


def _reader(settings_repo) -> tuple[UserSettings, StoreOverRows]:
    store = StoreOverRows(settings_repo.rows)
    return UserSettings(
        load_settings(),
        store,
        StubAsyncUserSettingsStore(),
        NoUserSettingsWrites(),
        cache_factory.cache(),
        cache_factory.async_cache(),
    ), store


def _scope():
    from new_core.identity import OwnerScope, SurfaceId

    return OwnerScope(owner_id=MINE, agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="c1")


def test_a_stored_override_reaches_a_reader_whose_scope_was_cached(settings_repo, registry):
    reader, store = _reader(settings_repo)
    assert reader.OUROBOROS_TIMEZONE(_scope()) == load_settings().OUROBOROS_TIMEZONE

    with _client(settings_repo, registry) as client:
        assert (
            client.put(
                "/main/OUROBOROS_TIMEZONE", json={"value": "Asia/Tokyo"}, headers={OWNER_HEADER: MINE}
            ).status_code
            == 200
        )

    assert reader.OUROBOROS_TIMEZONE(_scope()) == "Asia/Tokyo"
    assert store.reads == 2


def test_a_cleared_override_reaches_a_reader_whose_scope_was_cached(settings_repo, registry):
    reader, _store = _reader(settings_repo)

    with _client(settings_repo, registry) as client:
        client.put("/main/OUROBOROS_TIMEZONE", json={"value": "Asia/Tokyo"}, headers={OWNER_HEADER: MINE})
        assert reader.OUROBOROS_TIMEZONE(_scope()) == "Asia/Tokyo"
        assert client.delete("/main/OUROBOROS_TIMEZONE", headers={OWNER_HEADER: MINE}).json()["cleared"] is True

    assert reader.OUROBOROS_TIMEZONE(_scope()) == load_settings().OUROBOROS_TIMEZONE


def test_user_skill_allowlist_updates_and_clear_drop_its_exact_cache_entry(settings_repo, registry):
    key = "OUROBOROS_USER_ENABLED_SKILL_IDS"
    reader, store = _reader(settings_repo)
    assert reader.OUROBOROS_USER_ENABLED_SKILL_IDS(_scope()) == []

    with _client(settings_repo, registry) as client:
        client.put(f"/main/{key}", json={"value": "distribution:alpha"}, headers={OWNER_HEADER: MINE})
        assert reader.OUROBOROS_USER_ENABLED_SKILL_IDS(_scope()) == ["distribution:alpha"]
        client.delete(f"/main/{key}", headers={OWNER_HEADER: MINE})

    assert reader.OUROBOROS_USER_ENABLED_SKILL_IDS(_scope()) == []
    assert store.reads == 3


def test_clearing_an_absent_override_still_drops_the_entry(settings_repo, registry):
    reader, store = _reader(settings_repo)
    reader.OUROBOROS_TIMEZONE(_scope())

    with _client(settings_repo, registry) as client:
        assert client.delete("/main/OUROBOROS_TIMEZONE", headers={OWNER_HEADER: MINE}).json()["cleared"] is False

    reader.OUROBOROS_TIMEZONE(_scope())
    assert store.reads == 2


def test_a_failing_invalidation_fails_the_request(settings_repo, registry):
    with _client(settings_repo, registry, cache=FailingCache()) as client:
        written = client.put("/main/OUROBOROS_MODEL", json={"value": "mine"}, headers={OWNER_HEADER: MINE})
        cleared = client.delete("/main/OUROBOROS_MODEL", headers={OWNER_HEADER: MINE})

    assert (written.status_code, cleared.status_code) == (503, 503)


def test_the_writer_path_touches_no_synchronous_engine(settings_repo, registry, monkeypatch):
    import config_registry.user_settings.infra.repositories.repo as repo_module
    import ouroboros.storage.runtime_engine as runtime_engine

    def _refuse(*_args, **_kwargs):
        raise AssertionError("the writer reached the synchronous runtime engine")

    monkeypatch.setattr(runtime_engine, "resolve_runtime_engine", _refuse)
    monkeypatch.setattr(repo_module, "resolve_runtime_engine", _refuse)

    with _client(settings_repo, registry) as client:
        written = client.put("/main/OUROBOROS_MODEL", json={"value": "mine"}, headers={OWNER_HEADER: MINE})
        cleared = client.delete("/main/OUROBOROS_MODEL", headers={OWNER_HEADER: MINE})

    assert (written.status_code, cleared.status_code) == (200, 200)


def test_no_cache_module_names_the_synchronous_engine():
    cache_modules = [
        REPO_ROOT / "config_registry" / "user_settings" / "infra" / "cache.py",
        *sorted(path for path in (REPO_ROOT / "infra" / "cache").rglob("*.py") if "__pycache__" not in path.parts),
    ]
    assert len(cache_modules) > 1
    assert all(path.is_file() for path in cache_modules)
    source = "\n".join(path.read_text(encoding="utf-8") for path in cache_modules)

    assert "resolve_runtime_engine" not in source
    assert "runtime_engine" not in source
