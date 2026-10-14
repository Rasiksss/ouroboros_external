from __future__ import annotations

import logging
from typing import assert_never

from infra.security_context.interface import (
    ISecurityContextStore,
    SecurityContext,
    SecurityParameterKey,
    SecurityParameterType,
)
from infra.user_tokens.interface import IUserTokenStore, UserTokenTarget, UserTokenTargetKind
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.egress.auth.interfaces import EgressAuthDependencies, EgressAuthRefused, IEgressAuthenticator
from ouroboros.storage.settings.schema import (
    FORWARDABLE_HEADER_VALUE_RE,
    EgressAuth,
    EgressEntry,
    MTLSAuth,
    NoAuth,
    UserHeadersAuth,
    UserTokenAuth,
)

log = logging.getLogger(__name__)

RECORDING_SURFACES = frozenset({SurfaceId.BROWSER})
PERSONAL_TOKEN_SURFACES = frozenset({SurfaceId.BROWSER, SurfaceId.DIALOGS, SurfaceId.SKILL_BRIDGE})
_TARGET_NOUNS = {UserTokenTargetKind.MCP_SERVER: "server", UserTokenTargetKind.A2A_AGENT: "agent"}


class NoAuthenticator(IEgressAuthenticator):
    @property
    def forwarded_headers(self) -> frozenset[str]:
        return frozenset()

    async def request_headers(self, scope: OwnerScope) -> dict[str, str]:
        return {}


class UserHeadersAuthenticator(IEgressAuthenticator):
    def __init__(self, auth: UserHeadersAuth, store: ISecurityContextStore) -> None:
        self._store = store
        self._headers = tuple((name, SecurityParameterKey(SecurityParameterType.HEADER, name)) for name in auth.headers)
        self._cookies = tuple((name, SecurityParameterKey(SecurityParameterType.COOKIE, name)) for name in auth.cookies)
        self._keys = frozenset(key for _, key in (*self._headers, *self._cookies))
        self._forwarded_headers = frozenset({*auth.headers, "Cookie"})

    @property
    def forwarded_headers(self) -> frozenset[str]:
        return self._forwarded_headers

    async def request_headers(self, scope: OwnerScope) -> dict[str, str]:
        if scope.surface_id not in RECORDING_SURFACES:
            raise EgressAuthRefused(f"user credentials are not collected on surface {scope.surface_id.value}")
        context = await self._load(scope)
        headers = {name: self._header(context, name, key) for name, key in self._headers}
        if self._cookies:
            headers["Cookie"] = self._cookie(context)
        return headers

    async def _load(self, scope: OwnerScope) -> SecurityContext:
        try:
            return await self._store.load(scope.security_token, self._keys)
        except Exception as exc:
            log.warning("The security context store failed to load user credentials: %s", type(exc).__name__)
            raise EgressAuthRefused("the user credentials store is unavailable") from exc

    def _header(self, context: SecurityContext, name: str, key: SecurityParameterKey) -> str:
        value = context.value(key)
        if not value:
            raise EgressAuthRefused(f"the user credentials lack the header {name!r}")
        return self._forwardable(value, f"the header {name!r}")

    def _cookie(self, context: SecurityContext) -> str:
        pairs = {name: context.value(key) for name, key in self._cookies}
        missing = [name for name, value in pairs.items() if not value]
        if missing:
            raise EgressAuthRefused(f"the user credentials lack the cookie {missing[0]!r}")
        return "; ".join(self._forwardable(f"{name}={value}", f"the cookie {name!r}") for name, value in pairs.items())

    def _forwardable(self, value: str, what: str) -> str:
        if not FORWARDABLE_HEADER_VALUE_RE.fullmatch(value):
            raise EgressAuthRefused(f"the user credentials carry {what} with characters a request header cannot hold")
        return value


class UserTokenAuthenticator(IEgressAuthenticator):
    def __init__(self, target: UserTokenTarget, auth: UserTokenAuth, store: IUserTokenStore) -> None:
        self._target = target
        self._target_noun = _TARGET_NOUNS[target.kind]
        self._auth_header = auth.auth_header
        self._static_token = auth.auth_token
        self._store = store
        self._forwarded_headers = frozenset({auth.auth_header, "Cookie"})

    @property
    def forwarded_headers(self) -> frozenset[str]:
        return self._forwarded_headers

    async def request_headers(self, scope: OwnerScope) -> dict[str, str]:
        looked_up = scope.surface_id in PERSONAL_TOKEN_SURFACES
        personal = await self._personal_token(scope) if looked_up else None
        if personal:
            return {self._auth_header: self._forwardable(personal)}
        if self._static_token is not None:
            return {self._auth_header: self._static_token}
        if not looked_up:
            raise EgressAuthRefused(
                f"no user token is looked up on surface {scope.surface_id.value} "
                f"and the {self._target_noun} declares no static auth_token"
            )
        raise EgressAuthRefused(
            "no user token is stored for this owner and agent "
            f"and the {self._target_noun} declares no static auth_token"
        )

    async def _personal_token(self, scope: OwnerScope) -> str | None:
        try:
            return await self._store.load(self._target, scope.owner_id, scope.agent_id)
        except Exception as exc:
            log.warning("The user token store failed to load a user token: %s", type(exc).__name__)
            raise EgressAuthRefused("the user token store is unavailable") from exc

    def _forwardable(self, token: str) -> str:
        if not FORWARDABLE_HEADER_VALUE_RE.fullmatch(token):
            raise EgressAuthRefused(
                f"the stored user token for the header {self._auth_header!r} "
                "carries characters a request header cannot hold"
            )
        return token


def authenticator_for(
    entry: EgressEntry, target_kind: UserTokenTargetKind, dependencies: EgressAuthDependencies
) -> IEgressAuthenticator:
    auth = entry.auth
    match auth:
        case NoAuth() | MTLSAuth():
            return NoAuthenticator()
        case UserHeadersAuth():
            return UserHeadersAuthenticator(auth, dependencies.security_context_store)
        case UserTokenAuth() if not entry.takes_personal_token:
            return NoAuthenticator()
        case UserTokenAuth():
            return UserTokenAuthenticator(UserTokenTarget(target_kind, entry.id), auth, dependencies.user_token_store)
    assert_never(auth)


def static_headers(auth: EgressAuth) -> dict[str, str]:
    if isinstance(auth, UserTokenAuth) and auth.auth_token:
        return {auth.auth_header: auth.auth_token}
    return {}


__all__ = [
    "PERSONAL_TOKEN_SURFACES",
    "RECORDING_SURFACES",
    "NoAuthenticator",
    "UserHeadersAuthenticator",
    "UserTokenAuthenticator",
    "authenticator_for",
    "static_headers",
]
