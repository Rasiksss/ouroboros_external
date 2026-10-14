from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import config_registry.user_settings.api.http.endpoint as endpoint
from config_registry.user_settings.api.http.endpoint import OWNER_SETTING_KEYS
from config_registry.user_settings.interfaces import IAsyncUserSettingsStore, IUserSettingsRepo
from config_registry.user_settings.reader import UserSettings
from infra.cache import cache_factory
from infra.cache.null import AsyncNullCache, NullCache
from ouroboros.storage.settings.store import load_settings
from tests.helpers.agent_registry import FakeAgentRegistry
from tests.helpers.user_settings import NoUserSettingsWrites, StubUserSettingsStore

OWNER_HEADER = "X-Ouroboros-Owner-Id"
MINE = "owner-a"
THEIRS = "owner-b"


class FakeSettingsRepo(IUserSettingsRepo):
    """Keyed exactly as the SQL statements are, so isolation is visible in the keys."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], str] = {}

    async def set_override(self, owner_id: str, agent_id: str, key: str, value: str) -> None:
        self.rows[(owner_id, agent_id, key)] = value

    async def clear_override(self, owner_id: str, agent_id: str, key: str) -> bool:
        return self.rows.pop((owner_id, agent_id, key), None) is not None


class AsyncStoreOverRepoRows(IAsyncUserSettingsStore):
    """The reader's async face over the same rows the writer's fake holds."""

    def __init__(self, repo: FakeSettingsRepo) -> None:
        self._repo = repo

    async def get_setting_values(self, owner_id: str, agent_id: str) -> dict[str, str]:
        return {
            key: value
            for (owner, agent, key), value in self._repo.rows.items()
            if (owner, agent) == (owner_id, agent_id)
        }

    async def get_setting_value(self, owner_id: str, agent_id: str, key: str) -> str | None:
        return self._repo.rows.get((owner_id, agent_id, key))


@pytest.fixture
def settings_repo() -> FakeSettingsRepo:
    return FakeSettingsRepo()


@pytest.fixture(autouse=True)
def _no_cache_composed():
    """The writer's routes build their invalidation handle off the factory; compose none."""
    cache_factory.initialize("none", 300)


@pytest.fixture
def registry() -> FakeAgentRegistry:
    """Each owner already has a default agent, as they would after their first ingress."""
    fake = FakeAgentRegistry()
    for owner in ("global", MINE, THEIRS):
        _run(fake.get_or_create_default_agent(owner))
    return fake


def _run(coro):
    return asyncio.run(coro)


def _client(settings_repo, registry) -> TestClient:
    app = FastAPI()
    app.include_router(endpoint.router)
    app.dependency_overrides[endpoint.get_registry] = lambda: registry
    app.dependency_overrides[endpoint.get_settings_repo] = lambda: settings_repo
    app.dependency_overrides[endpoint.get_settings] = lambda: UserSettings(
        load_settings(),
        StubUserSettingsStore(),
        AsyncStoreOverRepoRows(settings_repo),
        NoUserSettingsWrites(),
        NullCache(),
        AsyncNullCache(),
    )
    return TestClient(app)


@pytest.mark.usefixtures("multiuser_mode")
def test_a_request_with_no_owner_header_is_refused_in_the_multiuser_mode(settings_repo, registry):
    with _client(settings_repo, registry) as client:
        assert client.get("/main").status_code == 403
        assert client.put("/main/OUROBOROS_TIMEZONE", json={"value": "Asia/Tokyo"}).status_code == 403
        assert client.delete("/main/OUROBOROS_TIMEZONE").status_code == 403
    assert settings_repo.rows == {}


def test_the_configured_owner_serves_the_single_owner_deployment(settings_repo, registry):
    instance = load_settings()
    with _client(settings_repo, registry) as client:
        response = client.get("/main")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == set(OWNER_SETTING_KEYS)
    assert body["OUROBOROS_TIMEZONE"] == instance.OUROBOROS_TIMEZONE
    assert body["OUROBOROS_MEMORY_DISTILL"] is instance.OUROBOROS_MEMORY_DISTILL


def test_an_override_is_reported_typed_in_force(settings_repo, registry):
    instance = load_settings()
    with _client(settings_repo, registry) as client:
        client.put("/main/OUROBOROS_TIMEZONE", json={"value": "Asia/Tokyo"}, headers={OWNER_HEADER: MINE})
        client.put("/main/OUROBOROS_MEMORY_DISTILL", json={"value": "false"}, headers={OWNER_HEADER: MINE})
        client.put("/main/OUROBOROS_EFFORT_TASK", json={"value": "high"}, headers={OWNER_HEADER: MINE})
        body = client.get("/main", headers={OWNER_HEADER: MINE}).json()

    assert body["OUROBOROS_TIMEZONE"] == "Asia/Tokyo"
    assert body["OUROBOROS_MEMORY_DISTILL"] is False
    assert body["OUROBOROS_EFFORT_TASK"] == "high"
    assert body["OUROBOROS_LOCALE"] == instance.OUROBOROS_LOCALE
    assert body["OUROBOROS_MODEL"] == instance.OUROBOROS_MODEL


def test_mcp_server_ids_round_trip_as_a_string_setting_and_clear_to_instance_default(settings_repo, registry):
    with _client(settings_repo, registry) as client:
        written = client.put(
            "/main/OUROBOROS_USER_ENABLED_MCP_IDS",
            json={"value": '["search", "calendar"]'},
            headers={OWNER_HEADER: MINE},
        )
        selected = client.get("/main", headers={OWNER_HEADER: MINE})
        cleared = client.delete("/main/OUROBOROS_USER_ENABLED_MCP_IDS", headers={OWNER_HEADER: MINE})
        denied = client.get("/main", headers={OWNER_HEADER: MINE})

    assert written.status_code == 200
    assert selected.json()["OUROBOROS_USER_ENABLED_MCP_IDS"] == ["search", "calendar"]
    assert cleared.json() == {"key": "OUROBOROS_USER_ENABLED_MCP_IDS", "cleared": True}
    assert denied.json()["OUROBOROS_USER_ENABLED_MCP_IDS"] == []


def test_user_enabled_skill_ids_round_trip_and_clear_to_the_empty_instance_default(settings_repo, registry):
    key = "OUROBOROS_USER_ENABLED_SKILL_IDS"
    with _client(settings_repo, registry) as client:
        written = client.put(
            f"/main/{key}",
            json={"value": "distribution:alpha, distribution:beta, distribution:alpha"},
            headers={OWNER_HEADER: MINE},
        )
        selected = client.get("/main", headers={OWNER_HEADER: MINE})
        cleared = client.delete(f"/main/{key}", headers={OWNER_HEADER: MINE})
        denied = client.get("/main", headers={OWNER_HEADER: MINE})

    assert written.json() == {
        "key": key,
        "value": "distribution:alpha, distribution:beta, distribution:alpha",
        "overridden": True,
    }
    assert selected.json()[key] == ["distribution:alpha", "distribution:beta"]
    assert cleared.json() == {"key": key, "cleared": True}
    assert denied.json()[key] == []


def test_user_enabled_skill_ids_are_isolated_by_owner_and_agent(settings_repo, registry):
    key = "OUROBOROS_USER_ENABLED_SKILL_IDS"
    sub = _run(registry.create_agent(MINE, "sub", "")).agent_id
    with _client(settings_repo, registry) as client:
        client.put(f"/main/{key}", json={"value": "distribution:mine"}, headers={OWNER_HEADER: MINE})
        client.put(f"/{sub}/{key}", json={"value": "distribution:sub"}, headers={OWNER_HEADER: MINE})
        client.put(f"/main/{key}", json={"value": "distribution:theirs"}, headers={OWNER_HEADER: THEIRS})

        mine = client.get("/main", headers={OWNER_HEADER: MINE}).json()[key]
        sub_selection = client.get(f"/{sub}", headers={OWNER_HEADER: MINE}).json()[key]
        theirs = client.get("/main", headers={OWNER_HEADER: THEIRS}).json()[key]

    assert (mine, sub_selection, theirs) == (["distribution:mine"], ["distribution:sub"], ["distribution:theirs"])


def test_a_key_outside_the_declaration_is_unreachable(settings_repo, registry):
    with _client(settings_repo, registry) as client:
        written = client.put("/main/OUROBOROS_DB_URL", json={"value": "postgres://x"}, headers={OWNER_HEADER: MINE})
        cleared = client.delete("/main/OUROBOROS_DB_URL", headers={OWNER_HEADER: MINE})
    assert (written.status_code, cleared.status_code) == (400, 400)
    assert settings_repo.rows == {}


def test_a_value_is_stored_as_the_text_it_arrived_as(settings_repo, registry):
    with _client(settings_repo, registry) as client:
        written = client.put("/main/OUROBOROS_MEMORY_DISTILL", json={"value": "maybe"}, headers={OWNER_HEADER: MINE})
    assert written.status_code == 200
    assert settings_repo.rows == {(MINE, "main", "OUROBOROS_MEMORY_DISTILL"): "maybe"}


def test_a_stored_switch_the_reader_does_not_recognise_fails_the_read(settings_repo, registry):
    """The surface stores unvalidated text, so a switch outside the set surfaces here, not silently."""
    with _client(settings_repo, registry) as client:
        client.put("/main/OUROBOROS_MEMORY_DISTILL", json={"value": "maybe"}, headers={OWNER_HEADER: MINE})
        with pytest.raises(ValueError, match="boolean setting value"):
            client.get("/main", headers={OWNER_HEADER: MINE})


def test_a_stored_effort_the_environment_would_refuse_is_the_instance_value_in_force(settings_repo, registry):
    instance = load_settings()
    with _client(settings_repo, registry) as client:
        client.put("/main/OUROBOROS_EFFORT_TASK", json={"value": "banana"}, headers={OWNER_HEADER: MINE})
        body = client.get("/main", headers={OWNER_HEADER: MINE}).json()
    assert body["OUROBOROS_EFFORT_TASK"] == instance.OUROBOROS_EFFORT_TASK


def test_a_header_naming_an_agent_has_no_effect(settings_repo, registry):
    sub = _run(registry.create_agent(MINE, "sub", "")).agent_id
    _run(settings_repo.set_override(MINE, sub, "OUROBOROS_TIMEZONE", "Asia/Tokyo"))
    with _client(settings_repo, registry) as client:
        listed = client.get("/main", headers={OWNER_HEADER: MINE, "X-Ouroboros-Agent-Id": sub}).json()
    assert listed["OUROBOROS_TIMEZONE"] == load_settings().OUROBOROS_TIMEZONE


def test_an_agent_the_owner_does_not_have_is_not_found(settings_repo, registry):
    theirs = _run(registry.create_agent(THEIRS, "theirs", "")).agent_id
    with _client(settings_repo, registry) as client:
        read = client.get(f"/{theirs}", headers={OWNER_HEADER: MINE})
        written = client.put(f"/{theirs}/OUROBOROS_MODEL", json={"value": "mine"}, headers={OWNER_HEADER: MINE})
        cleared = client.delete(f"/{theirs}/OUROBOROS_MODEL", headers={OWNER_HEADER: MINE})
    assert (read.status_code, written.status_code, cleared.status_code) == (404, 404, 404)
    assert settings_repo.rows == {}


def test_an_owner_writing_under_their_own_agent_reaches_only_their_own_rows(settings_repo, registry):
    with _client(settings_repo, registry) as client:
        client.put("/main/OUROBOROS_MODEL", json={"value": "mine"}, headers={OWNER_HEADER: MINE})
        client.put("/main/OUROBOROS_MODEL", json={"value": "theirs"}, headers={OWNER_HEADER: THEIRS})
        mine = client.get("/main", headers={OWNER_HEADER: MINE}).json()
        theirs = client.get("/main", headers={OWNER_HEADER: THEIRS}).json()

    assert set(settings_repo.rows) == {(MINE, "main", "OUROBOROS_MODEL"), (THEIRS, "main", "OUROBOROS_MODEL")}
    assert mine["OUROBOROS_MODEL"] == "mine"
    assert theirs["OUROBOROS_MODEL"] == "theirs"


def test_two_agents_of_one_owner_hold_independent_values(settings_repo, registry):
    sub = _run(registry.create_agent(MINE, "sub", "")).agent_id
    instance = load_settings()
    with _client(settings_repo, registry) as client:
        client.put("/main/OUROBOROS_TIMEZONE", json={"value": "Asia/Tokyo"}, headers={OWNER_HEADER: MINE})
        main = client.get("/main", headers={OWNER_HEADER: MINE}).json()
        other = client.get(f"/{sub}", headers={OWNER_HEADER: MINE}).json()
    assert main["OUROBOROS_TIMEZONE"] == "Asia/Tokyo"
    assert other["OUROBOROS_TIMEZONE"] == instance.OUROBOROS_TIMEZONE


def test_clearing_an_override_returns_the_instance_value(settings_repo, registry):
    with _client(settings_repo, registry) as client:
        client.put("/main/OUROBOROS_TIMEZONE", json={"value": "Asia/Tokyo"}, headers={OWNER_HEADER: MINE})
        cleared = client.delete("/main/OUROBOROS_TIMEZONE", headers={OWNER_HEADER: MINE})
        body = client.get("/main", headers={OWNER_HEADER: MINE}).json()

    assert cleared.json() == {"key": "OUROBOROS_TIMEZONE", "cleared": True}
    assert body["OUROBOROS_TIMEZONE"] == load_settings().OUROBOROS_TIMEZONE


def test_clearing_what_is_not_there_is_an_absence_and_not_a_failure(settings_repo, registry):
    with _client(settings_repo, registry) as client:
        client.put("/main/OUROBOROS_LOCALE", json={"value": "en-US"}, headers={OWNER_HEADER: MINE})
        response = client.delete("/main/OUROBOROS_TIMEZONE", headers={OWNER_HEADER: MINE})
        body = client.get("/main", headers={OWNER_HEADER: MINE}).json()

    assert response.status_code == 200
    assert response.json() == {"key": "OUROBOROS_TIMEZONE", "cleared": False}
    assert body["OUROBOROS_LOCALE"] == "en-US"


def test_the_api_document_describes_exactly_the_served_operations(settings_repo, registry):
    with _client(settings_repo, registry) as client:
        document = client.get("/openapi.json").json()
    assert {(path, method) for (path, item) in document["paths"].items() for method in item} == {
        ("/{agent_id}", "get"),
        ("/{agent_id}/{key}", "put"),
        ("/{agent_id}/{key}", "delete"),
        ("/{agent_id}/mcp-servers", "get"),
        ("/{agent_id}/mcp-servers/{server_id}/token", "put"),
        ("/{agent_id}/mcp-servers/{server_id}/token", "delete"),
        ("/{agent_id}/a2a-agents", "get"),
        ("/{agent_id}/a2a-agents/{remote_agent_id}/token", "put"),
        ("/{agent_id}/a2a-agents/{remote_agent_id}/token", "delete"),
        ("/{agent_id}/skills", "get"),
    }
