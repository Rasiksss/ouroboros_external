from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from infra.user_tokens.interface import UserTokenTarget, UserTokenTargetKind
from infra.user_tokens.sql import SqlUserTokenStore
from ouroboros.app.egress_auth_composition import EgressAuthComposition, EgressAuthCompositionError, EgressList
from ouroboros.egress.auth.authenticators import UserTokenAuthenticator, authenticator_for
from ouroboros.storage.settings.schema import (
    A2aRemoteAgentEntry,
    McpServerEntry,
    MTLSAuth,
    NoAuth,
    UserHeadersAuth,
    UserTokenAuth,
)
from tests.helpers.egress_auth import null_egress_auth_dependencies
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import declaration_of
from tests.helpers.user_tokens import seed_user_token

KEY = SecretStr(base64.b64encode(b"k" * 64).decode("ascii"))
DECLARATION = declaration_of(headers=("X-Sudir-Token",), cookies=("sudir_session",))


def _entry(server_id: str, auth, *, transport: str = "streamable_http", enabled: bool = True) -> McpServerEntry:
    return McpServerEntry(
        id=server_id, url=f"https://{server_id}.internal/mcp", transport=transport, auth=auth, enabled=enabled
    )


def _agent(agent_id: str, auth, *, enabled: bool) -> A2aRemoteAgentEntry:
    return A2aRemoteAgentEntry(id=agent_id, url=f"https://{agent_id}.internal/a2a", auth=auth, enabled=enabled)


def _static(auth_token: str | None = "Bearer static") -> UserTokenAuth:
    return UserTokenAuth(auth_header="X-API-Key", auth_token=auth_token)


def test_only_enabled_user_token_servers_over_streamable_http_take_a_personal_token() -> None:
    servers = [
        _entry("hr", _static(None)),
        _entry("crm", _static()),
        _entry("legacy", _static(), transport="sse"),
        _entry("paused", _static(), enabled=False),
        _entry("open", NoAuth()),
        _entry("gateway", MTLSAuth()),
        _entry("portal", UserHeadersAuth(headers=["X-Sudir-Token"])),
    ]

    assert EgressAuthComposition.personal_token_targets(servers) == ["hr", "crm"]


def test_personal_token_targets_cover_mcp_and_a2a_entries() -> None:
    entries = [
        _entry("hr", _static(None)),
        _entry("legacy", _static(), transport="sse"),
        _agent("research", _static(None), enabled=True),
        _agent("billing", _static(), enabled=True),
        _agent("paused", _static(), enabled=False),
        _agent("open", NoAuth(), enabled=True),
        _agent("gateway", MTLSAuth(), enabled=True),
        _agent("portal", UserHeadersAuth(headers=["X-Sudir-Token"]), enabled=True),
    ]

    assert EgressAuthComposition.personal_token_targets(entries) == ["hr", "research", "billing"]


@parametrize_from_dict(
    {
        "mcp servers": {
            "entries": [
                _entry("hr", _static(None)),
                _entry("crm", _static()),
                _entry("legacy", _static(), transport="sse"),
                _entry("open", NoAuth()),
                _entry("gateway", MTLSAuth()),
                _entry("portal", UserHeadersAuth(headers=["X-Sudir-Token"])),
            ],
            "target_kind": UserTokenTargetKind.MCP_SERVER,
        },
        "a2a agents": {
            "entries": [
                _agent("research", _static(None), enabled=True),
                _agent("billing", _static(), enabled=True),
                _agent("open", NoAuth(), enabled=True),
                _agent("gateway", MTLSAuth(), enabled=True),
                _agent("portal", UserHeadersAuth(headers=["X-Sudir-Token"]), enabled=True),
            ],
            "target_kind": UserTokenTargetKind.A2A_AGENT,
        },
    }
)
def test_the_runtime_reads_personal_tokens_for_exactly_the_entries_the_surface_offers_them_for(
    entries, target_kind
) -> None:
    reading = [
        entry.id
        for entry in entries
        if isinstance(authenticator_for(entry, target_kind, null_egress_auth_dependencies()), UserTokenAuthenticator)
    ]

    assert reading == EgressAuthComposition.personal_token_targets(entries)


@parametrize_from_dict(
    {
        "no entries": {"entries": []},
        "open": {"entries": [_entry("open", NoAuth())]},
        "mtls": {"entries": [_entry("gateway", MTLSAuth())]},
        "user headers": {"entries": [_entry("portal", UserHeadersAuth(headers=["X-Sudir-Token"]))]},
        "user token over sse": {"entries": [_entry("legacy", _static(), transport="sse")]},
        "disabled streamable user token": {"entries": [_entry("paused", _static(), enabled=False)]},
        "open agent": {"entries": [_agent("open", NoAuth(), enabled=True)]},
        "mtls agent": {"entries": [_agent("gateway", MTLSAuth(), enabled=True)]},
        "user headers agent": {"entries": [_agent("portal", UserHeadersAuth(headers=["X-Sudir-Token"]), enabled=True)]},
        "disabled user token agent": {"entries": [_agent("paused", _static(), enabled=False)]},
    }
)
def test_a_list_without_a_personal_token_entry_still_installs_the_sql_store(entries, engine_factory) -> None:
    store = EgressAuthComposition.user_token_store(entries, engine_factory=engine_factory, encryption_key=KEY)

    assert isinstance(store, SqlUserTokenStore)


def test_an_unusable_key_refuses_the_root_even_without_a_personal_token_entry(engine_factory) -> None:
    with pytest.raises(EgressAuthCompositionError, match="OUROBOROS_CREDENTIALS_ENCRYPTION_KEY") as refused:
        EgressAuthComposition.user_token_store(
            [_entry("open", NoAuth()), _agent("gateway", MTLSAuth(), enabled=True)],
            engine_factory=engine_factory,
            encryption_key=SecretStr(""),
        )

    assert str(refused.value).endswith(": none)")


def test_a_streamable_user_token_server_installs_the_sql_store_over_the_engine(runtime_db_url, engine_factory) -> None:
    hr = UserTokenTarget(UserTokenTargetKind.MCP_SERVER, "hr")
    seed_user_token(
        runtime_db_url,
        KEY,
        target=hr,
        owner_id="alice",
        agent_id="main",
        token="personal-token",
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
    )

    store = EgressAuthComposition.user_token_store(
        [_entry("hr", _static())], engine_factory=engine_factory, encryption_key=KEY
    )

    assert isinstance(store, SqlUserTokenStore)
    assert asyncio.get_event_loop().run_until_complete(store.load(hr, "alice", "main")) == "personal-token"


def test_a_user_token_a2a_agent_installs_the_sql_store(runtime_db_url, engine_factory) -> None:
    research = UserTokenTarget(UserTokenTargetKind.A2A_AGENT, "research")
    seed_user_token(
        runtime_db_url,
        KEY,
        target=research,
        owner_id="alice",
        agent_id="main",
        token="personal-a2a-token",
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
    )

    store = EgressAuthComposition.user_token_store(
        [_agent("research", _static(None), enabled=True)], engine_factory=engine_factory, encryption_key=KEY
    )

    loop = asyncio.get_event_loop()
    assert isinstance(store, SqlUserTokenStore)
    assert loop.run_until_complete(store.load(research, "alice", "main")) == "personal-a2a-token"
    assert (
        loop.run_until_complete(
            store.load(UserTokenTarget(UserTokenTargetKind.MCP_SERVER, "research"), "alice", "main")
        )
        is None
    )


_UNUSABLE_KEYS = {
    "empty": {"raw": ""},
    "not base64": {"raw": "not-a-key-but-a-secret!"},
    "short": {"raw": base64.b64encode(b"s" * 16).decode("ascii")},
    "a chat encryption key": {"raw": base64.b64encode(b"s" * 32).decode("ascii")},
}


@parametrize_from_dict(_UNUSABLE_KEYS)
def test_an_unusable_key_stops_the_root_naming_the_servers_and_the_setting_but_not_the_key(raw, engine_factory) -> None:
    servers = [_entry("hr", _static()), _entry("crm", _static(None)), _entry("open", NoAuth())]

    with pytest.raises(EgressAuthCompositionError) as refused:
        EgressAuthComposition.user_token_store(servers, engine_factory=engine_factory, encryption_key=SecretStr(raw))

    message = str(refused.value)
    assert "hr, crm" in message
    assert "open" not in message
    assert "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY" in message
    assert not raw or raw not in message


@parametrize_from_dict(_UNUSABLE_KEYS)
def test_an_unusable_key_names_every_personal_token_entry_of_both_lists(raw, engine_factory) -> None:
    entries = [
        _entry("hr", _static()),
        _entry("legacy", _static(), transport="sse"),
        _agent("research", _static(None), enabled=True),
    ]

    with pytest.raises(EgressAuthCompositionError) as refused:
        EgressAuthComposition.user_token_store(entries, engine_factory=engine_factory, encryption_key=SecretStr(raw))

    message = str(refused.value)
    assert message.endswith(": hr, research)")
    assert "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY" in message
    assert "legacy" not in message
    assert not raw or raw not in message


def test_every_undeclared_forwarded_name_is_listed_with_its_entry_and_type() -> None:
    entries = [
        _entry("portal", UserHeadersAuth(headers=["x-sudir-token", "X-Other"], cookies=["sudir_session", "Theme"])),
        _entry("mixed", UserHeadersAuth(headers=["Sudir_Session"], cookies=["SUDIR_SESSION"])),
        _entry("paused", UserHeadersAuth(headers=["X-Paused"]), enabled=False),
        _entry("hr", _static()),
        _agent("research", UserHeadersAuth(headers=["X-Research"], cookies=["sudir_session"]), enabled=True),
        _agent("dormant", UserHeadersAuth(headers=["X-Dormant"]), enabled=False),
    ]

    assert EgressAuthComposition.undeclared_user_headers(entries, DECLARATION) == [
        "portal header:x-other",
        "portal cookie:Theme",
        "mixed header:sudir_session",
        "mixed cookie:SUDIR_SESSION",
        "research header:x-research",
    ]


def test_undeclared_mcp_names_refuse_the_root_naming_the_mcp_list() -> None:
    servers = [_entry("portal", UserHeadersAuth(headers=["X-Other"], cookies=["theme"]))]

    with pytest.raises(EgressAuthCompositionError) as refused:
        EgressAuthComposition.refuse_undeclared_user_headers(servers, DECLARATION, source=EgressList.MCP)

    assert str(refused.value) == (
        "MCP servers forward user credentials the header collector does not declare: "
        "portal header:x-other, portal cookie:theme; no call to them could carry these values, so declare each in "
        "security_config/auth/header_collector.yaml or stop forwarding it in security_config/mcp.py"
    )


def test_undeclared_a2a_names_are_refused_naming_the_a2a_list() -> None:
    agents = [
        _agent("research", UserHeadersAuth(headers=["X-Sudir-Token", "X-Other"]), enabled=True),
        _agent("billing", _static("Bearer a2a-static-secret"), enabled=True),
    ]

    with pytest.raises(EgressAuthCompositionError) as refused:
        EgressAuthComposition.refuse_undeclared_user_headers(agents, DECLARATION, source=EgressList.A2A)

    message = str(refused.value)
    assert "research header:x-other" in message
    assert "security_config/a2a.py" in message
    assert "security_config/mcp.py" not in message
    assert "x-sudir-token" not in message
    assert "billing" not in message
    assert "https://" not in message
    assert ".internal" not in message
    assert "a2a-static-secret" not in message


@parametrize_from_dict({"mcp": {"source": EgressList.MCP}, "a2a": {"source": EgressList.A2A}})
def test_declared_forwarded_names_do_not_refuse_the_root(source) -> None:
    entries = [
        _entry("portal", UserHeadersAuth(headers=["X-Sudir-Token"], cookies=["sudir_session"])),
        _agent("research", UserHeadersAuth(headers=["X-Sudir-Token"], cookies=["sudir_session"]), enabled=True),
    ]

    assert EgressAuthComposition.undeclared_user_headers_refusal(entries, DECLARATION, source=source) is None
    EgressAuthComposition.refuse_undeclared_user_headers(entries, DECLARATION, source=source)
