from __future__ import annotations

import asyncio
from typing import get_args

from infra.user_tokens.interface import UserTokenTarget, UserTokenTargetKind
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.egress.auth.authenticators import UserTokenAuthenticator, authenticator_for, static_headers
from ouroboros.egress.auth.interfaces import EgressAuthDependencies, IEgressAuthenticator
from ouroboros.storage.settings.schema import (
    A2aRemoteAgentEntry,
    EgressAuth,
    EgressAuthBase,
    EgressAuthKind,
    McpServerEntry,
    MTLSAuth,
    NoAuth,
    UserHeadersAuth,
    UserTokenAuth,
)
from tests.helpers.egress_auth import null_egress_auth_dependencies
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import NullSecurityContextStore
from tests.helpers.user_tokens import FixedUserTokenStore


def _entry(auth: EgressAuth) -> McpServerEntry:
    return McpServerEntry(id="srv", url="https://egress.test/rpc", auth=auth)


def test_every_authorization_kind_has_an_authenticator() -> None:
    members = get_args(get_args(EgressAuth)[0])
    samples = {
        NoAuth: NoAuth(),
        UserHeadersAuth: UserHeadersAuth(headers=["X-A"]),
        UserTokenAuth: UserTokenAuth(auth_header="X-API-Key"),
        MTLSAuth: MTLSAuth(),
    }

    assert set(members) == set(samples)
    assert {member.model_fields["kind"].default for member in members} == set(EgressAuthKind)
    assert all(issubclass(member, EgressAuthBase) for member in members)
    assert all(
        isinstance(
            authenticator_for(_entry(auth), UserTokenTargetKind.MCP_SERVER, null_egress_auth_dependencies()),
            IEgressAuthenticator,
        )
        for auth in samples.values()
    )


def test_an_a2a_user_token_entry_maps_to_the_user_token_authenticator() -> None:
    store = FixedUserTokenStore({(UserTokenTarget(UserTokenTargetKind.MCP_SERVER, "crm"), "alice", "main"): "mcp-17"})
    entry = A2aRemoteAgentEntry(
        id="crm",
        url="https://agent.example.test/a2a",
        auth=UserTokenAuth(auth_header="X-API-Key", auth_token="static-secret-42"),
    )
    dependencies = EgressAuthDependencies(security_context_store=NullSecurityContextStore(), user_token_store=store)

    authenticator = authenticator_for(entry, UserTokenTargetKind.A2A_AGENT, dependencies)
    headers = asyncio.run(
        authenticator.request_headers(
            OwnerScope(owner_id="alice", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="1")
        )
    )

    assert isinstance(authenticator, UserTokenAuthenticator)
    assert authenticator.forwarded_headers == frozenset({"X-API-Key", "Cookie"})
    assert headers == {"X-API-Key": "static-secret-42"}
    assert store.loads == [(UserTokenTarget(UserTokenTargetKind.A2A_AGENT, "crm"), "alice", "main")]


static_header_cases = {
    "a static user token": {
        "auth": UserTokenAuth(auth_header="X-Key", auth_token="static-secret-42"),
        "expected": {"X-Key": "static-secret-42"},
    },
    "a user token without a static one": {"auth": UserTokenAuth(auth_header="X-Key"), "expected": {}},
    "a user token with an empty static one": {
        "auth": UserTokenAuth(auth_header="X-Key", auth_token=""),
        "expected": {},
    },
    "user headers": {"auth": UserHeadersAuth(headers=["X-Key"], cookies=["sudir"]), "expected": {}},
    "no authorization": {"auth": NoAuth(), "expected": {}},
    "mtls": {"auth": MTLSAuth(), "expected": {}},
}


@parametrize_from_dict(static_header_cases)
def test_static_headers_come_only_from_a_static_user_token(auth, expected) -> None:
    assert static_headers(auth) == expected
