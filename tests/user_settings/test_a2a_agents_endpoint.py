from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import config_registry.user_settings.api.http.endpoint as endpoint
from config_registry.user_settings.reader import UserSettings
from infra.async_engine import async_engine_factory, create_async_runtime_engine
from infra.cache.null import AsyncNullCache, NullCache
from infra.user_tokens.interface import UserTokenTarget, UserTokenTargetKind
from infra.user_tokens.sql import SqlUserTokenStore
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.egress.auth.authenticators import authenticator_for
from ouroboros.egress.auth.interfaces import EgressAuthDependencies
from ouroboros.storage.settings.schema import (
    A2aRemoteAgentEntry,
    McpServerEntry,
    MTLSAuth,
    NoAuth,
    UserHeadersAuth,
    UserTokenAuth,
)
from ouroboros.storage.settings.store import load_settings
from tests.helpers.agent_registry import FakeAgentRegistry
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import TEST_CHAT_ENCRYPTION_KEY, TEST_CREDENTIALS_KEY, NullSecurityContextStore
from tests.helpers.user_settings import NoUserSettingsWrites, StubAsyncUserSettingsStore, StubUserSettingsStore
from tests.helpers.user_tokens import FakeUserTokenRepo

OWNER_HEADER = "X-Ouroboros-Owner-Id"
MINE = "owner-a"
THEIRS = "owner-b"
CONFIGURED = "global"
SECRET = "personal-a2a-s3cr3t"
STATIC = "deployment-a2a-static"
UNSEALABLE = "personal A2A tokens cannot be sealed: OUROBOROS_CREDENTIALS_ENCRYPTION_KEY is not 64 bytes of base64"

RESEARCH = A2aRemoteAgentEntry(
    id="research",
    name="Research",
    url="https://research.example.internal/a2a",
    auth=UserTokenAuth(auth_header="Authorization", auth_token=STATIC),
)
AGENTS = [
    RESEARCH,
    A2aRemoteAgentEntry(
        id="personal",
        name="Personal",
        url="https://personal.example.internal/a2a",
        auth=UserTokenAuth(auth_header="X-API-Key"),
    ),
    A2aRemoteAgentEntry(
        id="headers",
        name="Headers",
        url="https://headers.example.internal/a2a",
        auth=UserHeadersAuth(headers=["X-Sudir-Token"]),
    ),
    A2aRemoteAgentEntry(id="plain", name="Plain", url="https://plain.example.internal/a2a"),
    A2aRemoteAgentEntry(id="gateway", name="Gateway", url="http://egress.example.internal/a2a", auth=MTLSAuth()),
    A2aRemoteAgentEntry(name="Unnamed", url="https://unnamed.example.internal/a2a"),
    A2aRemoteAgentEntry(
        id="disabled",
        name="Disabled",
        enabled=False,
        url="https://disabled.example.internal/a2a",
        auth=UserTokenAuth(auth_header="X-API-Key"),
    ),
]


def _run(coro):
    return asyncio.run(coro)


def _in(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _remote(remote_agent_id: str) -> UserTokenTarget:
    return UserTokenTarget(UserTokenTargetKind.A2A_AGENT, remote_agent_id)


def _server(server_id: str) -> UserTokenTarget:
    return UserTokenTarget(UserTokenTargetKind.MCP_SERVER, server_id)


@pytest.fixture
def registry() -> FakeAgentRegistry:
    fake = FakeAgentRegistry()
    for owner in (CONFIGURED, MINE, THEIRS):
        _run(fake.get_or_create_default_agent(owner))
    return fake


@pytest.fixture
def tokens() -> FakeUserTokenRepo:
    return FakeUserTokenRepo()


def _offer(monkeypatch: pytest.MonkeyPatch, agents: list[A2aRemoteAgentEntry], *, egress_enabled: bool) -> None:
    monkeypatch.setattr("security_config.a2a.a2a_remote_agents", agents)
    monkeypatch.setenv("A2A_EGRESS_ENABLED", "true" if egress_enabled else "false")


@pytest.fixture
def offered(monkeypatch: pytest.MonkeyPatch) -> None:
    _offer(monkeypatch, AGENTS, egress_enabled=True)


def _app(registry: FakeAgentRegistry, tokens: FakeUserTokenRepo | None) -> FastAPI:
    app = FastAPI()
    app.include_router(endpoint.router)
    app.dependency_overrides[endpoint.get_registry] = lambda: registry
    if tokens is not None:
        app.dependency_overrides[endpoint.get_user_token_repo] = lambda: tokens
        app.dependency_overrides[endpoint.get_a2a_user_token_repo] = lambda: tokens
    return app


def _headers(owner: str | None) -> dict[str, str]:
    return {OWNER_HEADER: owner} if owner else {}


def _put(client: TestClient, owner: str | None, remote_agent_id: str, body: object) -> httpx.Response:
    return client.put(f"/main/a2a-agents/{remote_agent_id}/token", json=body, headers=_headers(owner))


def _delete(client: TestClient, owner: str | None, remote_agent_id: str) -> httpx.Response:
    return client.delete(f"/main/a2a-agents/{remote_agent_id}/token", headers=_headers(owner))


def _listing(client: TestClient, owner: str | None) -> list[dict]:
    response = client.get("/main/a2a-agents", headers=_headers(owner))
    assert response.status_code == 200, response.text
    return response.json()["agents"]


def _expiries(agents: list[dict]) -> dict[str, datetime | None]:
    return {
        agent["id"]: None
        if agent["personal_token_expires_at"] is None
        else datetime.fromisoformat(agent["personal_token_expires_at"])
        for agent in agents
    }


@pytest.mark.usefixtures("offered")
def test_the_listing_reports_each_connectable_agent_and_its_personal_token(registry, tokens):
    stored = _in(600)
    _run(tokens.save(_remote("research"), MINE, "main", SECRET, stored))
    _run(tokens.save(_remote("headers"), MINE, "main", SECRET, _in(600)))

    with TestClient(_app(registry, tokens)) as client:
        agents = _listing(client, MINE)

    flags = ("name", "auth_kind", "personal_token_supported", "personal_token_writable", "has_static_token")
    assert [(agent["id"], *(agent[flag] for flag in flags)) for agent in agents] == [
        ("research", "Research", "user_token", True, True, True),
        ("personal", "Personal", "user_token", True, True, False),
        ("headers", "Headers", "user_headers", False, False, False),
        ("plain", "Plain", "none", False, False, False),
        ("gateway", "Gateway", "mtls", False, False, False),
    ]
    assert _expiries(agents) == {"research": stored, "personal": None, "headers": None, "plain": None, "gateway": None}
    assert {frozenset(agent) for agent in agents} == {frozenset({"id", *flags, "personal_token_expires_at"})}


def test_the_listing_is_empty_behind_the_egress_kill_switch(monkeypatch, registry, tokens):
    with TestClient(_app(registry, tokens)) as client:
        _offer(monkeypatch, AGENTS, egress_enabled=False)
        switched_off = client.get("/main/a2a-agents", headers=_headers(MINE))
        _offer(monkeypatch, AGENTS, egress_enabled=True)
        switched_on = _listing(client, MINE)

    assert switched_off.json() == {"agents": []}
    assert [agent["id"] for agent in switched_on] == ["research", "personal", "headers", "plain", "gateway"]


def test_an_id_declared_twice_is_listed_once(monkeypatch, registry, tokens):
    again = A2aRemoteAgentEntry(id="research", name="Research again", url="https://again.example.internal/a2a")
    _offer(monkeypatch, [RESEARCH, again], egress_enabled=True)

    with TestClient(_app(registry, tokens)) as client:
        agents = _listing(client, MINE)

    assert [(agent["id"], agent["name"], agent["auth_kind"]) for agent in agents] == [
        ("research", "Research", "user_token")
    ]


@pytest.mark.usefixtures("offered")
def test_an_expired_token_is_not_reported(registry, tokens):
    _run(tokens.save(_remote("research"), MINE, "main", SECRET, datetime.now(UTC) - timedelta(seconds=1)))

    with TestClient(_app(registry, tokens)) as client:
        agents = _listing(client, MINE)

    assert _expiries(agents)["research"] is None


@pytest.mark.usefixtures("offered")
def test_the_listing_carries_no_address_and_no_token(registry, tokens):
    _run(tokens.save(_remote("research"), MINE, "main", SECRET, _in(600)))

    with TestClient(_app(registry, tokens)) as client:
        response = client.get("/main/a2a-agents", headers=_headers(MINE))

    assert response.status_code == 200
    for value in (STATIC, SECRET, "example.internal", "https://", "http://"):
        assert value not in response.text


@pytest.mark.usefixtures("offered")
def test_the_configured_owner_cannot_write_but_can_clear(registry, tokens):
    held = ("configured-held-token", _in(600))
    tokens.rows[(_remote("research"), CONFIGURED, "main")] = held

    with TestClient(_app(registry, tokens)) as client:
        agents = _listing(client, None)
        implicit = _put(client, None, "research", {"token": SECRET, "ttl_sec": 600})
        explicit = _put(client, CONFIGURED, "research", {"token": SECRET, "ttl_sec": 600})
        after_writes = dict(tokens.rows)
        cleared = _delete(client, None, "research")

    assert {agent["id"]: agent["personal_token_writable"] for agent in agents} == dict.fromkeys(
        ["research", "personal", "headers", "plain", "gateway"], False
    )
    assert agents[0]["personal_token_supported"] is True
    assert (implicit.status_code, explicit.status_code) == (403, 403)
    assert SECRET not in implicit.text + explicit.text
    assert after_writes == {(_remote("research"), CONFIGURED, "main"): held}
    assert cleared.json() == {"remote_agent_id": "research", "cleared": True}
    assert tokens.rows == {}


@parametrize_from_dict(
    {
        "shortest ttl": {"body": {"token": SECRET, "ttl_sec": 60}, "status": 200},
        "longest ttl": {"body": {"token": SECRET, "ttl_sec": 7_776_000}, "status": 200},
        "longest token": {"body": {"token": "t" * 8192, "ttl_sec": 600}, "status": 200},
        "ttl below a minute": {"body": {"token": SECRET, "ttl_sec": 59}, "status": 422},
        "ttl above ninety days": {"body": {"token": SECRET, "ttl_sec": 7_776_001}, "status": 422},
        "token longer than 8192 characters": {"body": {"token": "t" * 8193, "ttl_sec": 600}, "status": 400},
    }
)
@pytest.mark.usefixtures("offered")
def test_ttl_and_length_are_bounded(registry, tokens, body, status):
    before = datetime.now(UTC)
    with TestClient(_app(registry, tokens)) as client:
        written = _put(client, MINE, "personal", body)
    after = datetime.now(UTC)

    assert written.status_code == status
    stored = {key: token for key, (token, _) in tokens.rows.items()}
    assert stored == ({(_remote("personal"), MINE, "main"): body["token"]} if status == 200 else {})
    for _, expires_at in tokens.rows.values():
        ttl = timedelta(seconds=body["ttl_sec"])
        assert before + ttl <= expires_at <= after + ttl


@parametrize_from_dict(
    {
        "empty": {"token": ""},
        "a line break": {"token": f"{SECRET}\nX-Injected: 1"},
        "a leading space": {"token": f" {SECRET}"},
        "a non-ascii character": {"token": f"{SECRET}ж"},
    }
)
@pytest.mark.usefixtures("offered")
def test_a_token_a_header_cannot_hold_is_refused_and_not_echoed(registry, tokens, token):
    with TestClient(_app(registry, tokens)) as client:
        written = _put(client, MINE, "research", {"token": token, "ttl_sec": 600})

    assert written.status_code == 400
    assert SECRET not in written.text
    assert tokens.rows == {}


@parametrize_from_dict(
    {
        "ttl missing": {"content": json.dumps({"token": SECRET})},
        "ttl not a number": {"content": json.dumps({"token": SECRET, "ttl_sec": "soon"})},
        "an unknown field": {"content": json.dumps({"token": "x", "ttl_sec": 600, "tokn": SECRET})},
        "a bare string": {"content": json.dumps(SECRET)},
        "not json": {"content": f'{{"token": "{SECRET}"'},
    }
)
@pytest.mark.usefixtures("offered")
def test_a_malformed_body_is_refused_without_input(registry, tokens, content):
    with TestClient(_app(registry, tokens)) as client:
        written = client.put("/main/a2a-agents/research/token", content=content, headers=_headers(MINE))

    assert written.status_code == 422
    assert SECRET not in written.text
    assert tokens.rows == {}


@parametrize_from_dict(
    {
        "forwarded user headers": {"remote_agent_id": "headers"},
        "no authorization": {"remote_agent_id": "plain"},
        "mutual tls": {"remote_agent_id": "gateway"},
    }
)
@pytest.mark.usefixtures("offered")
def test_an_agent_that_takes_no_personal_token_refuses_the_write(registry, tokens, remote_agent_id):
    with TestClient(_app(registry, tokens)) as client:
        written = _put(client, MINE, remote_agent_id, {"token": SECRET, "ttl_sec": 600})

    assert written.status_code == 400
    assert SECRET not in written.text
    assert tokens.rows == {}


@parametrize_from_dict(
    {
        "undeclared": {"remote_agent_id": "unknown", "egress_enabled": True},
        "disabled": {"remote_agent_id": "disabled", "egress_enabled": True},
        "declared with no id": {"remote_agent_id": " ", "egress_enabled": True},
        "the kill switch is off": {"remote_agent_id": "research", "egress_enabled": False},
    }
)
def test_an_unknown_or_disabled_agent_is_not_found(monkeypatch, registry, tokens, remote_agent_id, egress_enabled):
    _offer(monkeypatch, AGENTS, egress_enabled=egress_enabled)

    with TestClient(_app(registry, tokens)) as client:
        written = _put(client, MINE, remote_agent_id, {"token": SECRET, "ttl_sec": 600})

    assert (written.status_code, written.json()) == (404, {"detail": "no such A2A agent"})
    assert SECRET not in written.text
    assert tokens.rows == {}


@parametrize_from_dict(
    {
        "the agent is offered one": {"agents": AGENTS, "egress_enabled": True},
        "the kill switch is off": {"agents": AGENTS, "egress_enabled": False},
        "the agent is disabled": {
            "agents": [RESEARCH.model_copy(update={"enabled": False})],
            "egress_enabled": True,
        },
        "the agent no longer takes one": {
            "agents": [RESEARCH.model_copy(update={"auth": NoAuth()})],
            "egress_enabled": True,
        },
    }
)
def test_a_stored_token_stays_revocable(monkeypatch, registry, tokens, agents, egress_enabled):
    _offer(monkeypatch, agents, egress_enabled=egress_enabled)
    _run(tokens.save(_remote("research"), MINE, "main", SECRET, _in(600)))

    with TestClient(_app(registry, tokens)) as client:
        first = _delete(client, MINE, "research")
        second = _delete(client, MINE, "research")

    assert first.json() == {"remote_agent_id": "research", "cleared": True}
    assert second.json() == {"remote_agent_id": "research", "cleared": False}
    assert tokens.rows == {}


@pytest.mark.usefixtures("offered")
def test_another_owners_token_is_neither_listed_nor_cleared(registry, tokens):
    theirs_held = (SECRET, _in(600))
    tokens.rows[(_remote("research"), THEIRS, "main")] = theirs_held
    theirs_agent = _run(registry.create_agent(THEIRS, "theirs", "")).agent_id
    tokens.rows[(_remote("research"), THEIRS, theirs_agent)] = theirs_held
    sub = _run(registry.create_agent(MINE, "sub", "")).agent_id
    sub_held = (SECRET, _in(600))
    tokens.rows[(_remote("personal"), MINE, sub)] = sub_held

    with TestClient(_app(registry, tokens)) as client:
        mine = _listing(client, MINE)
        cleared = _delete(client, MINE, "research")
        written = _put(client, MINE, "research", {"token": "mine-token", "ttl_sec": 600})
        foreign = (
            client.get(f"/{theirs_agent}/a2a-agents", headers=_headers(MINE)),
            client.put(
                f"/{theirs_agent}/a2a-agents/research/token",
                json={"token": "mine-token", "ttl_sec": 600},
                headers=_headers(MINE),
            ),
            client.delete(f"/{theirs_agent}/a2a-agents/research/token", headers=_headers(MINE)),
        )

    assert _expiries(mine) == dict.fromkeys(["research", "personal", "headers", "plain", "gateway"])
    assert cleared.json() == {"remote_agent_id": "research", "cleared": False}
    assert written.status_code == 200
    assert [response.status_code for response in foreign] == [404, 404, 404]
    assert tokens.rows[(_remote("research"), THEIRS, "main")] == theirs_held
    assert tokens.rows[(_remote("research"), THEIRS, theirs_agent)] == theirs_held
    assert tokens.rows[(_remote("personal"), MINE, sub)] == sub_held
    assert tokens.rows[(_remote("research"), MINE, "main")][0] == "mine-token"


def test_an_mcp_token_with_the_same_id_is_neither_listed_nor_touched(monkeypatch, registry, tokens):
    _offer(monkeypatch, [RESEARCH.model_copy(update={"id": "hr"})], egress_enabled=True)
    mcp_held = ("mcp-personal-token", _in(900))
    tokens.rows[(_server("hr"), MINE, "main")] = mcp_held
    app = _app(registry, tokens)
    app.dependency_overrides[endpoint.get_mcp_servers] = lambda: [
        McpServerEntry(id="hr", name="HR", url="https://hr.example.internal/mcp", auth=UserTokenAuth(auth_header="X")),
    ]
    app.dependency_overrides[endpoint.get_settings] = lambda: UserSettings(
        load_settings(),
        StubUserSettingsStore(),
        StubAsyncUserSettingsStore(by_pair={}),
        NoUserSettingsWrites(),
        NullCache(),
        AsyncNullCache(),
    )

    with TestClient(app) as client:
        before_a2a = _expiries(_listing(client, MINE))
        written = _put(client, MINE, "hr", {"token": SECRET, "ttl_sec": 600})
        listed_mcp = client.get("/main/mcp-servers", headers=_headers(MINE)).json()["servers"]
        cleared = _delete(client, MINE, "hr")
        after_clear = dict(tokens.rows)
        _put(client, MINE, "hr", {"token": SECRET, "ttl_sec": 600})
        client.delete("/main/mcp-servers/hr/token", headers=_headers(MINE))
        mcp_after = client.get("/main/mcp-servers", headers=_headers(MINE)).json()["servers"]
        a2a_after = _expiries(_listing(client, MINE))

    assert before_a2a == {"hr": None}
    assert written.status_code == 200
    assert _expiries(listed_mcp) == {"hr": mcp_held[1]}
    assert cleared.json() == {"remote_agent_id": "hr", "cleared": True}
    assert after_clear == {(_server("hr"), MINE, "main"): mcp_held}
    assert _expiries(mcp_after) == {"hr": None}
    assert a2a_after["hr"] == tokens.rows[(_remote("hr"), MINE, "main")][1]
    assert set(tokens.rows) == {(_remote("hr"), MINE, "main")}


@pytest.mark.usefixtures("offered")
def test_an_unusable_key_is_unavailable_naming_the_setting(monkeypatch, runtime_db_url, registry):
    unusable = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", TEST_CHAT_ENCRYPTION_KEY)
    monkeypatch.setenv("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY", unusable)
    async_engine_factory.initialize(runtime_db_url)

    with TestClient(_app(registry, None)) as client:
        responses = (
            client.get("/main/a2a-agents", headers=_headers(MINE)),
            _put(client, MINE, "research", {"token": SECRET, "ttl_sec": 600}),
            _delete(client, MINE, "research"),
        )

    assert [(response.status_code, response.json()) for response in responses] == [(503, {"detail": UNSEALABLE})] * 3
    assert all(unusable not in response.text and SECRET not in response.text for response in responses)


@pytest.mark.usefixtures("offered")
def test_a_token_written_over_http_is_what_the_a2a_authenticator_sends(monkeypatch, runtime_db_url, registry):
    monkeypatch.setenv("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY", TEST_CREDENTIALS_KEY)
    async_engine_factory.initialize(runtime_db_url)

    before = datetime.now(UTC)
    with TestClient(_app(registry, None)) as client:
        written = _put(client, MINE, "research", {"token": SECRET, "ttl_sec": 3600})
        agents = _listing(client, MINE)
    after = datetime.now(UTC)

    body = written.json()
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert (written.status_code, body["remote_agent_id"]) == (200, "research")
    assert before + timedelta(hours=1) <= expires_at <= after + timedelta(hours=1)
    assert _expiries(agents)["research"] == expires_at
    assert SECRET not in written.text
    sent, mcp_loaded = _run(_sent_and_mcp_loaded(runtime_db_url))
    assert sent == {"Authorization": SECRET}
    assert mcp_loaded is None


async def _sent_and_mcp_loaded(db_url: str) -> tuple[dict[str, str], str | None]:
    engine = create_async_runtime_engine(db_url)
    try:
        store = SqlUserTokenStore(engine, encryption_key=load_settings().OUROBOROS_CREDENTIALS_ENCRYPTION_KEY)
        dependencies = EgressAuthDependencies(security_context_store=NullSecurityContextStore(), user_token_store=store)
        authenticator = authenticator_for(RESEARCH, UserTokenTargetKind.A2A_AGENT, dependencies)
        scope = OwnerScope(owner_id=MINE, agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="1")
        return await authenticator.request_headers(scope), await store.load(_server("research"), MINE, "main")
    finally:
        await engine.dispose()
