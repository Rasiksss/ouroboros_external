from __future__ import annotations

import asyncio

import pytest

from infra.user_tokens.interface import UserTokenTarget, UserTokenTargetKind
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.egress.auth.authenticators import PERSONAL_TOKEN_SURFACES, UserTokenAuthenticator
from ouroboros.egress.auth.interfaces import EgressAuthRefused
from ouroboros.storage.settings.schema import UserTokenAuth
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.user_tokens import FixedUserTokenStore

SERVER = "hr"
MCP_SERVER = UserTokenTarget(UserTokenTargetKind.MCP_SERVER, SERVER)
A2A_AGENT = UserTokenTarget(UserTokenTargetKind.A2A_AGENT, SERVER)
HEADER = "X-API-Key"
PERSONAL = "personal-secret-17"
STATIC = "static-secret-42"
SECRETS = (PERSONAL, STATIC, "backend-secret-99", "evil-value", "Injected")


def _scope(surface: SurfaceId = SurfaceId.BROWSER, *, owner: str = "alice", agent: str = "main") -> OwnerScope:
    return OwnerScope(owner_id=owner, agent_id=agent, surface_id=surface, chat_id="1")


def _authenticator(
    target: UserTokenTarget, store: FixedUserTokenStore, *, static: str | None = None
) -> UserTokenAuthenticator:
    return UserTokenAuthenticator(target, UserTokenAuth(auth_header=HEADER, auth_token=static), store)


def _tokens(token: str) -> FixedUserTokenStore:
    return FixedUserTokenStore({(MCP_SERVER, "alice", "main"): token})


def _headers(authenticator: UserTokenAuthenticator, scope: OwnerScope) -> dict[str, str]:
    return asyncio.run(authenticator.request_headers(scope))


def _refusal(authenticator: UserTokenAuthenticator, scope: OwnerScope) -> str:
    with pytest.raises(EgressAuthRefused) as caught:
        _headers(authenticator, scope)
    reason = caught.value.reason
    for secret in SECRETS:
        assert secret not in reason
    return reason


def test_a_personal_token_wins_over_the_static_one() -> None:
    assert _headers(_authenticator(MCP_SERVER, _tokens(PERSONAL), static=STATIC), _scope()) == {HEADER: PERSONAL}


def test_the_static_token_is_used_when_no_personal_token_is_stored() -> None:
    store = FixedUserTokenStore({})

    assert _headers(_authenticator(MCP_SERVER, store, static=STATIC), _scope()) == {HEADER: STATIC}
    assert store.loads == [(MCP_SERVER, "alice", "main")]


@parametrize_from_dict(
    {
        "an mcp server": {"target": MCP_SERVER, "other": A2A_AGENT},
        "an a2a agent": {"target": A2A_AGENT, "other": MCP_SERVER},
    }
)
def test_the_store_is_read_under_the_target(target, other) -> None:
    store = FixedUserTokenStore({(other, "alice", "main"): PERSONAL})

    assert _headers(_authenticator(target, store, static=STATIC), _scope()) == {HEADER: STATIC}
    assert store.loads == [(target, "alice", "main")]


def test_an_empty_personal_token_counts_as_absent() -> None:
    assert _headers(_authenticator(MCP_SERVER, _tokens(""), static=STATIC), _scope()) == {HEADER: STATIC}


def test_no_personal_and_no_static_token_is_refused_naming_nothing_secret() -> None:
    reason = _refusal(_authenticator(MCP_SERVER, FixedUserTokenStore({})), _scope())

    assert reason == "no user token is stored for this owner and agent and the server declares no static auth_token"


def test_an_a2a_target_refuses_naming_the_agent() -> None:
    store = FixedUserTokenStore({})
    authenticator = _authenticator(A2A_AGENT, store)

    stored = _refusal(authenticator, _scope())
    looked_up = _refusal(authenticator, _scope(SurfaceId.A2A))

    assert stored == "no user token is stored for this owner and agent and the agent declares no static auth_token"
    assert looked_up == "no user token is looked up on surface a2a and the agent declares no static auth_token"
    assert store.loads == [(A2A_AGENT, "alice", "main")]


def test_an_a2a_call_never_reads_the_store_and_carries_the_static_token() -> None:
    store = _tokens(PERSONAL)
    a2a = OwnerScope(owner_id="alice", agent_id="main", surface_id=SurfaceId.A2A, chat_id="-1")

    assert _headers(_authenticator(MCP_SERVER, store, static=STATIC), a2a) == {HEADER: STATIC}
    assert store.loads == []


def test_an_a2a_call_without_a_static_token_is_refused_naming_the_surface() -> None:
    store = _tokens(PERSONAL)

    reason = _refusal(_authenticator(MCP_SERVER, store), _scope(SurfaceId.A2A))

    assert reason == "no user token is looked up on surface a2a and the server declares no static auth_token"
    assert store.loads == []


lookup_cases = {
    "browser": {"surface": SurfaceId.BROWSER},
    "dialogs": {"surface": SurfaceId.DIALOGS},
    "skill bridge": {"surface": SurfaceId.SKILL_BRIDGE},
}


@parametrize_from_dict(lookup_cases)
def test_a_personal_surface_looks_up_the_token_by_server_owner_and_agent(surface) -> None:
    store = FixedUserTokenStore({(MCP_SERVER, "bob", "helper"): PERSONAL})
    scope = OwnerScope(owner_id="bob", agent_id="helper", surface_id=surface, chat_id="another-chat")

    assert _headers(_authenticator(MCP_SERVER, store), scope) == {HEADER: PERSONAL}
    assert store.loads == [(MCP_SERVER, "bob", "helper")]


def test_only_a2a_is_outside_the_personal_token_surfaces() -> None:
    assert set(SurfaceId) - PERSONAL_TOKEN_SURFACES == {SurfaceId.A2A}


def test_a_store_error_is_refused_without_falling_back_to_the_static_token(caplog) -> None:
    store = FixedUserTokenStore({}, error=RuntimeError("backend down backend-secret-99"))

    reason = _refusal(_authenticator(MCP_SERVER, store, static=STATIC), _scope())

    assert reason == "the user token store is unavailable"
    assert "backend-secret-99" not in caplog.text


def test_a_personal_token_a_header_cannot_hold_is_refused_without_falling_back() -> None:
    reason = _refusal(_authenticator(MCP_SERVER, _tokens("evil-value\r\nX-Injected: yes"), static=STATIC), _scope())

    assert "cannot hold" in reason
    assert HEADER in reason


def test_the_authenticator_forwards_its_header_and_the_cookie() -> None:
    assert _authenticator(MCP_SERVER, FixedUserTokenStore({})).forwarded_headers == frozenset({HEADER, "Cookie"})
