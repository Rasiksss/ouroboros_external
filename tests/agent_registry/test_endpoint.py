from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import config_registry.agent_registry.api.http.endpoint as endpoint
from config_registry.agent_registry.domain import AgentEntity
from infra.utils.http.fastapi import install_trace_middleware
from ouroboros.observability.aef_tracing import TRACE_HEADER_NAME
from tests.helpers.agent_registry import FakeAgentRegistry

OWNER_HEADER = "X-Ouroboros-Owner-Id"


@pytest.fixture
def registry() -> FakeAgentRegistry:
    return FakeAgentRegistry()


def _client(registry) -> TestClient:
    app = FastAPI()
    app.include_router(endpoint.router)
    install_trace_middleware(app, source="test")
    app.dependency_overrides[endpoint.get_registry] = lambda: registry
    return TestClient(app)


@pytest.mark.usefixtures("multiuser_mode")
def test_a_missing_header_is_refused_in_the_multiuser_mode(registry):
    with _client(registry) as client:
        response = client.get("/")
    assert response.status_code == 403
    assert OWNER_HEADER in response.json()["detail"]
    assert registry.by_owner == {}


@pytest.mark.usefixtures("multiuser_mode")
def test_an_empty_header_is_refused_the_same_way(registry):
    with _client(registry) as client:
        response = client.get("/", headers={OWNER_HEADER: "   "})
    assert response.status_code == 403


@pytest.mark.usefixtures("multiuser_mode")
def test_every_route_refuses_a_request_that_names_no_owner(registry):
    with _client(registry) as client:
        assert client.get("/").status_code == 403
        assert client.post("/", json={"name": "researcher"}).status_code == 403
        assert client.delete("/sub-1").status_code == 403


def test_the_configured_owner_serves_the_single_owner_deployment(registry):
    with _client(registry) as client:
        created = client.post("/", json={"name": "researcher", "description": "reads"})
        listed = client.get("/")
    assert created.status_code == 201
    assert created.json()["owner_id"] == "global"
    assert [agent["owner_id"] for agent in listed.json()] == ["global"]


def test_the_header_selects_the_owner_and_isolates_the_listings(registry):
    with _client(registry) as client:
        mine = client.post("/", json={"name": "mine"}, headers={OWNER_HEADER: "owner-a"}).json()
        theirs = client.post("/", json={"name": "theirs"}, headers={OWNER_HEADER: "owner-b"}).json()
        listed_a = client.get("/", headers={OWNER_HEADER: "owner-a"}).json()
        listed_b = client.get("/", headers={OWNER_HEADER: "owner-b"}).json()
    assert [agent["agent_id"] for agent in listed_a] == [mine["agent_id"]]
    assert [agent["agent_id"] for agent in listed_b] == [theirs["agent_id"]]


def test_an_absent_agent_answers_not_found_and_discloses_nothing(registry):
    with _client(registry) as client:
        theirs = client.post("/", json={"name": "theirs"}, headers={OWNER_HEADER: "owner-b"}).json()
        response = client.delete(f"/{theirs['agent_id']}", headers={OWNER_HEADER: "owner-a"})
    assert response.status_code == 404
    body = response.text
    assert "owner-b" not in body
    assert theirs["agent_id"] not in body
    assert registry.by_owner["owner-b"][0].is_active


def test_the_not_found_comes_from_the_deactivation_itself(registry):
    async def _unread(owner_id: str, agent_id: str) -> AgentEntity | None:
        raise AssertionError("the surface read the agent before deactivating it")

    registry.get_agent = _unread
    with _client(registry) as client:
        created = client.post("/", json={"name": "researcher"}).json()
        deactivated = client.delete(f"/{created['agent_id']}")
        missing = client.delete("/no-such-agent")
    assert deactivated.status_code == 204
    assert missing.status_code == 404


def test_deactivation_answers_with_no_content_and_flips_the_flag(registry):
    with _client(registry) as client:
        created = client.post("/", json={"name": "researcher"}).json()
        response = client.delete(f"/{created['agent_id']}")
        listed = client.get("/").json()
    assert response.status_code == 204
    assert response.content == b""
    assert [agent["is_active"] for agent in listed] == [False]


def test_the_default_agent_is_deactivated_like_any_other(registry):
    """404 and 204 are the route's only two answers; no id earns a conflict."""
    registry.by_owner["global"] = [
        AgentEntity(agent_id="main", owner_id="global", name="main", description="", is_active=True)
    ]
    with _client(registry) as client:
        response = client.delete("/main")
        listed = client.get("/").json()
    assert (response.status_code, response.content) == (204, b"")
    assert not registry.by_owner["global"][0].is_active
    assert [agent["is_active"] for agent in listed] == [False]


def test_the_api_document_describes_exactly_the_served_operations(registry):
    with _client(registry) as client:
        document = client.get("/openapi.json").json()
        served = {(path, method) for (path, item) in document["paths"].items() for method in item}
        assert served == {("/", "get"), ("/", "post"), ("/{agent_id}", "delete")}
        assert set(document["components"]["schemas"]["AgentPayload"]["properties"]) == {
            "agent_id",
            "owner_id",
            "name",
            "description",
            "is_active",
        }
        assert client.get("/").status_code == 200
        created = client.post("/", json={"name": "researcher"})
        assert created.status_code == 201
        assert client.delete(f"/{created.json()['agent_id']}").status_code == 204


def test_a_request_without_an_operation_identifier_has_one_originated(registry):
    with _client(registry) as client:
        response = client.get("/")
    trace_id = response.headers[TRACE_HEADER_NAME]
    assert len(trace_id) == 36 and trace_id.count("-") == 4


def test_a_well_formed_operation_identifier_is_accepted_rather_than_replaced(registry):
    supplied = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    with _client(registry) as client:
        response = client.get("/", headers={TRACE_HEADER_NAME: supplied})
    assert response.headers[TRACE_HEADER_NAME] == supplied


@pytest.mark.usefixtures("multiuser_mode")
def test_every_response_carries_the_operation_identifier_including_a_refusal(registry):
    with _client(registry) as client:
        refused = client.get("/")
        missing = client.delete("/no-such-agent", headers={OWNER_HEADER: "owner-a"})
    assert refused.status_code == 403
    assert refused.headers.get(TRACE_HEADER_NAME)
    assert missing.status_code == 404
    assert missing.headers.get(TRACE_HEADER_NAME)
