"""The current-chat routes use real SQL and the trusted browser identity."""

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from new_core.identity import OwnerScope
from ouroboros.scheduling.service import SchedulerService
from ouroboros.scheduling.store import SqlScheduleStore
from ouroboros.storage import runtime_persistence as persistence_module
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.agent_registry import FakeAgentRegistry
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.postgres import postgres_urls
from transport.browser.identity import ConfiguredOwnerIdentity, TrustedHeaderIdentity, browser_identity
from transport.browser.router import collect_routes


@pytest.fixture
def schedule_client(tmp_path):
    runtime_persistence.initialize()
    browser_identity.initialize(TrustedHeaderIdentity(FakeAgentRegistry()))
    composed_store = persistence_module.create_runtime_persistence(configured_urls=postgres_urls())
    service = SchedulerService(SqlScheduleStore(composed_store.engine), timezone_for=lambda _: "UTC")
    app = Starlette(routes=collect_routes(data_dir=tmp_path, repo_dir=tmp_path))
    app.state.scheduler = service
    try:
        with TestClient(app) as client:
            client.headers.update({"X-Ouroboros-Owner-Id": "mine", "X-Ouroboros-Chat-Id": "here"})
            yield client, service
    finally:
        composed_store.engine.dispose()
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


def _create(service, *, owner="mine", agent="main", chat="here", message="remember"):
    ctx = SimpleNamespace(scope=OwnerScope(owner_id=owner, agent_id=agent, chat_id=chat, surface_id="browser"))
    return service.create_reminder(ctx, message=message, in_minutes=10)


def test_list_paginates_the_current_chat_with_the_existing_public_projection(schedule_client):
    client, service = schedule_client
    own = [_create(service, message=f"reminder {n}") for n in range(3)]
    _create(service, owner="another")
    _create(service, agent="another")
    _create(service, chat="elsewhere")

    response = client.get("/api/chat/schedules?limit=2")
    assert response.status_code == 200
    first = response.json()
    assert (first["total"], first["counts"]["active"], first["next_cursor"]) == (3, 3, 2)
    second = client.get("/api/chat/schedules?limit=2&cursor=2").json()
    rows = first["schedules"] + second["schedules"]
    assert {row["id"] for row in rows} == {row["id"] for row in own}
    assert "next_cursor" not in second
    assert all(row["delivery"] == {"scope": {"surface_id": "browser", "chat_id": "here"}} for row in rows)
    assert all("fingerprint" not in row and row["summary"] for row in rows)


@parametrize_from_dict({axis: {"axis": axis} for axis in ("owner", "agent", "chat")})
def test_delete_hides_a_schedule_outside_the_current_scope(schedule_client, axis):
    client, service = schedule_client
    other = _create(service, **{axis: "elsewhere"})
    assert client.delete(f"/api/chat/schedules/{other['id']}?revision=999").status_code == 404
    assert service.store.get_for_dispatch(other["id"]) is not None


def test_delete_requires_the_current_revision_and_returns_empty_success(schedule_client):
    client, service = schedule_client
    own = _create(service)
    path = f"/api/chat/schedules/{own['id']}"
    assert client.delete(f"{path}?revision=999").status_code == 409
    response = client.delete(f"{path}?revision={own['revision']}")
    assert (response.status_code, response.content) == (204, b"")
    assert client.delete(f"{path}?revision={own['revision']}").status_code == 404
    assert client.get("/api/chat/schedules").json()["total"] == 0
    assert client.post("/api/chat/schedules", json={}).status_code == 405
    assert client.patch(path, json={}).status_code == 405


@parametrize_from_dict(
    {
        "missing revision": {"query": ""},
        "invalid revision": {"query": "?revision=no"},
        "zero revision": {"query": "?revision=0"},
        "overflowing revision": {"query": "?revision=" + "9" * 100},
    }
)
def test_delete_requires_a_positive_expected_revision(schedule_client, query):
    client, service = schedule_client
    own = _create(service)
    assert client.delete(f"/api/chat/schedules/{own['id']}{query}").status_code == 400
    assert client.get("/api/chat/schedules").json()["total"] == 1


@parametrize_from_dict(
    {
        "negative cursor": {"query": "cursor=-1"},
        "overflowing cursor": {"query": "cursor=" + "9" * 100},
        "empty page": {"query": "limit=0"},
    }
)
def test_list_refuses_invalid_pagination(schedule_client, query):
    client, _ = schedule_client
    assert client.get(f"/api/chat/schedules?{query}").status_code == 400


def test_schedule_routes_refuse_missing_trusted_identity(schedule_client):
    client, service = schedule_client
    own = _create(service)
    client.headers.clear()
    assert client.get("/api/chat/schedules").status_code == 403
    assert client.delete(f"/api/chat/schedules/{own['id']}?revision=1").status_code == 403
