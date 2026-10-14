from __future__ import annotations

import asyncio

from infra.user_tokens.interface import UserTokenTargetKind
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.egress.auth.authenticators import (
    NoAuthenticator,
    UserHeadersAuthenticator,
    UserTokenAuthenticator,
    authenticator_for,
)
from ouroboros.egress.auth.interfaces import EgressAuthDependencies
from ouroboros.storage.settings.schema import McpServerEntry, MTLSAuth, NoAuth, UserHeadersAuth, UserTokenAuth
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import FixedSecurityContextStore
from tests.helpers.user_tokens import FixedUserTokenStore


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _scope(surface: SurfaceId) -> OwnerScope:
    return OwnerScope(owner_id="o", agent_id="a", surface_id=surface, chat_id="1")


def _dependencies(store: FixedSecurityContextStore | None = None, tokens: FixedUserTokenStore | None = None):
    return EgressAuthDependencies(
        security_context_store=store if store is not None else FixedSecurityContextStore(),
        user_token_store=tokens if tokens is not None else FixedUserTokenStore({}),
    )


def _entry(auth, *, transport: str = "streamable_http", url: str = "https://mcp.test/mcp") -> McpServerEntry:
    return McpServerEntry(id="srv", transport=transport, url=url, auth=auth)


authenticator_cases = {
    "none": {"entry": _entry(NoAuth()), "expected": NoAuthenticator},
    "mtls over the egress gateway": {
        "entry": _entry(MTLSAuth(), url="http://egress-gateway.test/mcp"),
        "expected": NoAuthenticator,
    },
    "mtls over sse": {"entry": _entry(MTLSAuth(), transport="sse"), "expected": NoAuthenticator},
    "user headers": {"entry": _entry(UserHeadersAuth(headers=["X-A"])), "expected": UserHeadersAuthenticator},
    "a static user token over sse": {
        "entry": _entry(UserTokenAuth(auth_header="X-API-Key", auth_token="static"), transport="sse"),
        "expected": NoAuthenticator,
    },
    "a user token over streamable http": {
        "entry": _entry(UserTokenAuth(auth_header="X-API-Key")),
        "expected": UserTokenAuthenticator,
    },
}


@parametrize_from_dict(authenticator_cases)
def test_each_entry_is_served_by_its_authenticator(entry, expected) -> None:
    assert type(authenticator_for(entry, UserTokenTargetKind.MCP_SERVER, _dependencies())) is expected


def test_no_authenticator_returns_no_headers_forwards_nothing_and_never_reads_a_store() -> None:
    store = FixedSecurityContextStore()
    tokens = FixedUserTokenStore({})
    authenticator = authenticator_for(_entry(MTLSAuth()), UserTokenTargetKind.MCP_SERVER, _dependencies(store, tokens))

    assert _run(authenticator.request_headers(_scope(SurfaceId.A2A))) == {}
    assert authenticator.forwarded_headers == frozenset()
    assert store.loads == []
    assert tokens.loads == []
