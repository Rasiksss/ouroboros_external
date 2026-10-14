from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

import config_registry.user_settings.api.http.endpoint as endpoint
from config_registry.user_settings.reader import UserSettings
from infra.async_engine import async_engine_factory, create_async_runtime_engine
from infra.cache.null import AsyncNullCache, NullCache
from infra.user_tokens.interface import UserTokenTarget, UserTokenTargetKind
from infra.user_tokens.sql import SqlUserTokenStore
from ouroboros.storage.settings.schema import McpServerEntry, MTLSAuth, UserHeadersAuth, UserTokenAuth
from ouroboros.storage.settings.store import load_settings
from tests.helpers.agent_registry import FakeAgentRegistry
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import TEST_CHAT_ENCRYPTION_KEY
from tests.helpers.user_settings import NoUserSettingsWrites, StubAsyncUserSettingsStore, StubUserSettingsStore
from tests.helpers.user_tokens import FakeUserTokenRepo

OWNER_HEADER = "X-Ouroboros-Owner-Id"
MINE = "owner-a"
THEIRS = "owner-b"
CONFIGURED = "global"
SECRET = "personal-s3cr3t-token"
STATIC = "deployment-static-token"
KEY = base64.b64encode(b"k" * 64).decode("ascii")
UNSEALABLE = "personal MCP tokens cannot be sealed: OUROBOROS_CREDENTIALS_ENCRYPTION_KEY is not 64 bytes of base64"

SERVERS = [
    McpServerEntry(
        id="personal",
        name="Personal",
        url="https://personal.example.internal/mcp",
        auth=UserTokenAuth(auth_header="X-API-Key"),
    ),
    McpServerEntry(
        id="static",
        name="Static",
        url="https://static.example.internal/mcp",
        auth=UserTokenAuth(auth_header="X-API-Key", auth_token=STATIC),
    ),
    McpServerEntry(
        id="legacy-sse",
        name="Legacy SSE",
        transport="sse",
        url="https://sse.example.internal/sse",
        auth=UserTokenAuth(auth_header="X-API-Key", auth_token=STATIC),
    ),
    McpServerEntry(
        id="headers",
        name="Headers",
        url="https://headers.example.internal/mcp",
        auth=UserHeadersAuth(headers=["X-Sudir-Token"]),
    ),
    McpServerEntry(id="plain", name="Plain", url="https://plain.example.internal/mcp"),
    McpServerEntry(id="gateway", name="Gateway", url="http://egress.example.internal/mcp", auth=MTLSAuth()),
    McpServerEntry(
        id="disabled",
        name="Disabled",
        enabled=False,
        url="https://disabled.example.internal/mcp",
        auth=UserTokenAuth(auth_header="X-API-Key"),
    ),
]


def _server(server_id: str) -> UserTokenTarget:
    return UserTokenTarget(UserTokenTargetKind.MCP_SERVER, server_id)


def _run(coro):
    return asyncio.run(coro)


def _in(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


@pytest.fixture
def registry() -> FakeAgentRegistry:
    fake = FakeAgentRegistry()
    for owner in (CONFIGURED, MINE, THEIRS):
        _run(fake.get_or_create_default_agent(owner))
    return fake


@pytest.fixture
def tokens() -> FakeUserTokenRepo:
    return FakeUserTokenRepo()


def _settings(selections: dict[tuple[str, str], dict[str, str]]) -> UserSettings:
    return UserSettings(
        load_settings(),
        StubUserSettingsStore(),
        StubAsyncUserSettingsStore(by_pair=selections),
        NoUserSettingsWrites(),
        NullCache(),
        AsyncNullCache(),
    )


def _client(
    registry: FakeAgentRegistry,
    tokens: FakeUserTokenRepo | None,
    *,
    servers: list[McpServerEntry] = SERVERS,
    selections: dict[tuple[str, str], dict[str, str]] | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(endpoint.router)
    app.dependency_overrides[endpoint.get_registry] = lambda: registry
    app.dependency_overrides[endpoint.get_mcp_servers] = lambda: servers
    app.dependency_overrides[endpoint.get_settings] = lambda: _settings(selections or {})
    if tokens is not None:
        app.dependency_overrides[endpoint.get_user_token_repo] = lambda: tokens
    return TestClient(app)


def _put(client: TestClient, server_id: str, body: object, owner: str | None = MINE, agent: str = "main"):
    headers = {OWNER_HEADER: owner} if owner else {}
    return client.put(f"/{agent}/mcp-servers/{server_id}/token", json=body, headers=headers)


def _delete(client: TestClient, server_id: str, owner: str | None = MINE, agent: str = "main"):
    headers = {OWNER_HEADER: owner} if owner else {}
    return client.delete(f"/{agent}/mcp-servers/{server_id}/token", headers=headers)


def _listing(client: TestClient, owner: str | None = MINE, agent: str = "main") -> dict[str, dict]:
    headers = {OWNER_HEADER: owner} if owner else {}
    response = client.get(f"/{agent}/mcp-servers", headers=headers)
    assert response.status_code == 200, response.text
    return {server["id"]: server for server in response.json()["servers"]}


def _expiry(server: dict) -> datetime | None:
    raw = server["personal_token_expires_at"]
    return None if raw is None else datetime.fromisoformat(raw)


def test_the_listing_names_every_enabled_server_in_order_with_what_the_owner_may_do(registry, tokens):
    stored = _in(600)
    _run(tokens.save(_server("personal"), MINE, "main", SECRET, stored))
    _run(tokens.save(_server("legacy-sse"), MINE, "main", SECRET, _in(600)))
    selections = {(MINE, "main"): {"OUROBOROS_USER_ENABLED_MCP_IDS": '["static", "plain"]'}}

    with _client(registry, tokens, selections=selections) as client:
        response = client.get("/main/mcp-servers", headers={OWNER_HEADER: MINE})

    servers = response.json()["servers"]
    assert [server["id"] for server in servers] == ["personal", "static", "legacy-sse", "headers", "plain", "gateway"]
    by_id = {server["id"]: server for server in servers}
    flags = (
        "transport",
        "auth_kind",
        "selected",
        "personal_token_supported",
        "personal_token_writable",
        "has_static_token",
    )
    assert {server_id: tuple(server[flag] for flag in flags) for server_id, server in by_id.items()} == {
        "personal": ("streamable_http", "user_token", False, True, True, False),
        "static": ("streamable_http", "user_token", True, True, True, True),
        "legacy-sse": ("sse", "user_token", False, False, False, True),
        "headers": ("streamable_http", "user_headers", False, False, False, False),
        "plain": ("streamable_http", "none", True, False, False, False),
        "gateway": ("streamable_http", "mtls", False, False, False, False),
    }
    assert {server_id: _expiry(server) for server_id, server in by_id.items()} == {
        "personal": stored,
        "static": None,
        "legacy-sse": None,
        "headers": None,
        "plain": None,
        "gateway": None,
    }
    assert set(by_id["personal"]) == {"id", "name", *flags, "personal_token_expires_at"}
    assert STATIC not in response.text
    assert "example.internal" not in response.text


def test_the_listing_uses_the_instance_mcp_selection_without_an_override(registry, tokens, monkeypatch):
    monkeypatch.setenv("OUROBOROS_USER_ENABLED_MCP_IDS", '["plain"]')
    from ouroboros.storage.settings.bootstrap import bootstrap_config

    bootstrap_config(force=True)
    with _client(registry, tokens) as client:
        listed = _listing(client)

    assert listed["plain"]["selected"] is True
    assert listed["static"]["selected"] is False


def test_a_malformed_override_of_another_key_does_not_break_the_listing(registry, tokens):
    selections = {(MINE, "main"): {"OUROBOROS_USER_ENABLED_MCP_IDS": '["plain"]', "OUROBOROS_MEMORY_DISTILL": "maybe"}}

    with _client(registry, tokens, selections=selections) as client:
        listed = _listing(client)

    assert {server_id for server_id, server in listed.items() if server["selected"]} == {"plain"}


def test_the_configured_owner_may_not_write_a_personal_token(registry, tokens):
    with _client(registry, tokens) as client:
        listed = _listing(client, owner=None)
        written = _put(client, "personal", {"token": SECRET, "ttl_sec": 600}, owner=None)
        explicit = _put(client, "personal", {"token": SECRET, "ttl_sec": 600}, owner=CONFIGURED)

    assert {server_id: server["personal_token_writable"] for server_id, server in listed.items()} == dict.fromkeys(
        listed, False
    )
    assert listed["personal"]["personal_token_supported"] is True
    assert (written.status_code, explicit.status_code) == (403, 403)
    assert SECRET not in written.text
    assert tokens.rows == {}


def test_the_configured_owner_may_clear_a_personal_token(registry, tokens):
    _run(tokens.save(_server("personal"), CONFIGURED, "main", SECRET, _in(600)))

    with _client(registry, tokens) as client:
        cleared = _delete(client, "personal", owner=None)

    assert cleared.json() == {"server_id": "personal", "cleared": True}
    assert tokens.rows == {}


def test_an_expired_token_is_not_listed(registry, tokens):
    _run(tokens.save(_server("personal"), MINE, "main", SECRET, datetime.now(UTC) - timedelta(seconds=1)))

    with _client(registry, tokens) as client:
        listed = _listing(client)

    assert listed["personal"]["personal_token_expires_at"] is None


def test_the_listing_is_empty_when_the_deployment_lists_nothing(registry, tokens):
    with _client(registry, tokens, servers=[]) as client:
        response = client.get("/main/mcp-servers", headers={OWNER_HEADER: MINE})

    assert response.json() == {"servers": []}


def test_the_kill_switch_hands_the_surface_no_server(monkeypatch):
    import security_config.mcp

    monkeypatch.setattr(security_config.mcp, "mcp_servers", SERVERS)
    monkeypatch.setenv("MCP_ENABLED", "false")
    switched_off = endpoint.get_mcp_servers()
    monkeypatch.setenv("MCP_ENABLED", "true")
    switched_on = endpoint.get_mcp_servers()

    assert (list(switched_off), list(switched_on)) == ([], SERVERS)


def test_a_put_stores_the_token_for_the_owner_and_agent_until_the_ttl(registry, tokens):
    before = datetime.now(UTC)
    with _client(registry, tokens) as client:
        written = _put(client, "static", {"token": SECRET, "ttl_sec": 3600})
        listed = _listing(client)
    after = datetime.now(UTC)

    assert written.status_code == 200
    body = written.json()
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert body["server_id"] == "static"
    assert before + timedelta(seconds=3600) <= expires_at <= after + timedelta(seconds=3600)
    assert tokens.rows == {(_server("static"), MINE, "main"): (SECRET, expires_at)}
    assert SECRET not in written.text
    assert _expiry(listed["static"]) == expires_at


@parametrize_from_dict(
    {
        "shortest ttl": {"body": {"token": SECRET, "ttl_sec": 60}},
        "longest ttl": {"body": {"token": SECRET, "ttl_sec": 7_776_000}},
        "longest token": {"body": {"token": "t" * 8192, "ttl_sec": 600}},
        "token with inner spaces": {"body": {"token": "Bearer  abc", "ttl_sec": 600}},
    }
)
def test_a_put_accepts_the_bounds(registry, tokens, body):
    with _client(registry, tokens) as client:
        written = _put(client, "personal", body)

    assert written.status_code == 200
    assert tokens.rows[(_server("personal"), MINE, "main")][0] == body["token"]


@parametrize_from_dict(
    {
        "empty": {"token": ""},
        "longer than 8192 characters": {"token": SECRET + "a" * 8192},
        "a line break": {"token": f"{SECRET}\nX-Injected: 1"},
        "a leading space": {"token": f" {SECRET}"},
        "a non-ascii character": {"token": f"{SECRET}ж"},
    }
)
def test_a_token_a_header_cannot_hold_is_refused_without_echoing_it(registry, tokens, token):
    with _client(registry, tokens) as client:
        written = _put(client, "personal", {"token": token, "ttl_sec": 600})

    assert written.status_code == 400
    assert SECRET not in written.text
    assert tokens.rows == {}


@parametrize_from_dict(
    {
        "ttl below a minute": {"body": {"token": SECRET, "ttl_sec": 59}},
        "ttl above ninety days": {"body": {"token": SECRET, "ttl_sec": 7_776_001}},
        "ttl not a number": {"body": {"token": SECRET, "ttl_sec": "soon"}},
        "ttl missing": {"body": {"token": SECRET}},
        "token under another name": {"body": {"tokn": SECRET, "token": "x", "ttl_sec": 600}},
        "a bare string": {"body": SECRET},
    }
)
def test_a_malformed_body_is_unprocessable_and_never_echoes_the_token(registry, tokens, body):
    with _client(registry, tokens) as client:
        written = _put(client, "personal", body)

    assert written.status_code == 422
    assert SECRET not in written.text
    assert tokens.rows == {}


def test_a_body_that_is_not_json_is_unprocessable(registry, tokens):
    with _client(registry, tokens) as client:
        written = client.put(
            "/main/mcp-servers/personal/token", content=f'{{"token": "{SECRET}"', headers={OWNER_HEADER: MINE}
        )

    assert written.status_code == 422
    assert SECRET not in written.text


@parametrize_from_dict(
    {
        "static token over sse": {"server_id": "legacy-sse"},
        "forwarded user headers": {"server_id": "headers"},
        "no authorization": {"server_id": "plain"},
        "mutual tls": {"server_id": "gateway"},
    }
)
def test_a_server_that_takes_no_personal_token_refuses_a_write_and_clears_nothing(registry, tokens, server_id):
    with _client(registry, tokens) as client:
        written = _put(client, server_id, {"token": SECRET, "ttl_sec": 600})
        cleared = _delete(client, server_id)

    assert (written.status_code, cleared.json()) == (400, {"server_id": server_id, "cleared": False})
    assert tokens.rows == {}


@parametrize_from_dict(
    {
        "undeclared": {"server_id": "unknown"},
        "disabled": {"server_id": "disabled"},
    }
)
def test_a_server_outside_the_enabled_list_is_not_found_for_a_write(registry, tokens, server_id):
    with _client(registry, tokens) as client:
        written = _put(client, server_id, {"token": SECRET, "ttl_sec": 600})
        cleared = _delete(client, server_id)

    assert (written.status_code, cleared.json()) == (404, {"server_id": server_id, "cleared": False})
    assert tokens.rows == {}


@parametrize_from_dict(
    {
        "the kill switch is off": {"servers": []},
        "the server is disabled": {"servers": [entry for entry in SERVERS if entry.id != "personal"]},
    }
)
def test_a_stored_token_is_revoked_while_its_server_is_not_offered_one(registry, tokens, servers):
    _run(tokens.save(_server("personal"), MINE, "main", SECRET, _in(600)))

    with _client(registry, tokens, servers=servers) as client:
        cleared = _delete(client, "personal")

    assert cleared.json() == {"server_id": "personal", "cleared": True}
    assert tokens.rows == {}


def test_an_agent_the_owner_does_not_have_is_not_found(registry, tokens):
    theirs = _run(registry.create_agent(THEIRS, "theirs", "")).agent_id
    _run(tokens.save(_server("personal"), THEIRS, theirs, SECRET, _in(600)))

    with _client(registry, tokens) as client:
        listed = client.get(f"/{theirs}/mcp-servers", headers={OWNER_HEADER: MINE})
        written = _put(client, "personal", {"token": SECRET, "ttl_sec": 600}, agent=theirs)
        cleared = _delete(client, "personal", agent=theirs)

    assert (listed.status_code, written.status_code, cleared.status_code) == (404, 404, 404)
    assert set(tokens.rows) == {(_server("personal"), THEIRS, theirs)}


def test_a_delete_clears_the_token_and_reports_an_absence_after(registry, tokens):
    _run(tokens.save(_server("personal"), MINE, "main", SECRET, _in(600)))

    with _client(registry, tokens) as client:
        first = _delete(client, "personal")
        second = _delete(client, "personal")
        listed = _listing(client)

    assert first.json() == {"server_id": "personal", "cleared": True}
    assert second.json() == {"server_id": "personal", "cleared": False}
    assert listed["personal"]["personal_token_expires_at"] is None


def test_another_owners_token_is_neither_listed_nor_cleared(registry, tokens):
    theirs_expiry = _in(600)
    _run(tokens.save(_server("personal"), THEIRS, "main", SECRET, theirs_expiry))
    sub = _run(registry.create_agent(MINE, "sub", "")).agent_id
    _run(tokens.save(_server("static"), MINE, sub, SECRET, _in(600)))

    with _client(registry, tokens) as client:
        mine = _listing(client)
        cleared = _delete(client, "personal")
        _put(client, "personal", {"token": "mine-token", "ttl_sec": 600})

    assert {server_id: server["personal_token_expires_at"] for server_id, server in mine.items()} == dict.fromkeys(mine)
    assert cleared.json()["cleared"] is False
    assert tokens.rows[(_server("personal"), THEIRS, "main")] == (SECRET, theirs_expiry)
    assert tokens.rows[(_server("personal"), MINE, "main")][0] == "mine-token"
    assert (_server("static"), MINE, sub) in tokens.rows


@pytest.mark.usefixtures("multiuser_mode")
def test_a_request_with_no_owner_header_is_refused_in_the_multiuser_mode(registry, tokens):
    with _client(registry, tokens) as client:
        listed = client.get("/main/mcp-servers")
        written = _put(client, "personal", {"token": SECRET, "ttl_sec": 600}, owner=None)
        malformed = _put(client, "personal", {"token": SECRET}, owner=None)
        cleared = _delete(client, "personal", owner=None)

    assert (listed.status_code, written.status_code, malformed.status_code, cleared.status_code) == (403,) * 4
    assert tokens.rows == {}


def test_an_unusable_encryption_key_is_a_503_that_names_the_setting_only(monkeypatch, runtime_db_url):
    unusable = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setenv("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY", unusable)
    async_engine_factory.initialize(runtime_db_url)

    with pytest.raises(HTTPException) as refused:
        endpoint.get_user_token_repo()

    assert (refused.value.status_code, refused.value.detail) == (503, UNSEALABLE)
    assert unusable not in str(refused.value.detail)


def test_an_unusable_encryption_key_refuses_the_request(monkeypatch, runtime_db_url, registry):
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", TEST_CHAT_ENCRYPTION_KEY)
    monkeypatch.setenv("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY", "")
    async_engine_factory.initialize(runtime_db_url)

    with _client(registry, None) as client:
        listed = client.get("/main/mcp-servers", headers={OWNER_HEADER: MINE})
        written = _put(client, "personal", {"token": SECRET, "ttl_sec": 600})
        cleared = _delete(client, "personal")

    responses = (listed, written, cleared)
    assert [(response.status_code, response.json()) for response in responses] == [(503, {"detail": UNSEALABLE})] * 3
    assert SECRET not in written.text


def test_a_token_written_over_the_surface_is_the_one_the_authenticator_store_loads(
    monkeypatch, runtime_db_url, registry
):
    monkeypatch.setenv("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY", KEY)
    async_engine_factory.initialize(runtime_db_url)

    with _client(registry, None) as client:
        written = _put(client, "personal", {"token": SECRET, "ttl_sec": 600})
        listed = _listing(client)

    expires_at = datetime.fromisoformat(written.json()["expires_at"])
    assert _expiry(listed["personal"]) == expires_at
    assert _run(_load_sealed(runtime_db_url, _server("personal"), MINE, "main")) == SECRET


async def _load_sealed(db_url: str, target: UserTokenTarget, owner_id: str, agent_id: str) -> str | None:
    engine = create_async_runtime_engine(db_url)
    try:
        store = SqlUserTokenStore(engine, encryption_key=load_settings().OUROBOROS_CREDENTIALS_ENCRYPTION_KEY)
        return await store.load(target, owner_id, agent_id)
    finally:
        await engine.dispose()
