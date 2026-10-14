from __future__ import annotations

import base64
import secrets
import time
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from infra.security_context import security_context, security_context_collector
from infra.security_context.collector import DeclaredParametersCollector
from infra.security_context.declaration import HeaderCollectorDeclaration, SecurityParameterDeclaration
from infra.security_context.interface import (
    ISecurityContextStore,
    SecurityContext,
    SecurityParameter,
    SecurityParameterKey,
    SecurityParameterType,
)

TEST_CREDENTIALS_KEY = base64.b64encode(b"k" * 64).decode("ascii")
TEST_CHAT_ENCRYPTION_KEY = base64.b64encode(b"c" * 32).decode("ascii")


def sealed_under_the_chat_key(plaintext: str, *, associated_data: bytes) -> str:
    nonce = secrets.token_bytes(12)
    key = AESGCM(base64.b64decode(TEST_CHAT_ENCRYPTION_KEY))
    return base64.urlsafe_b64encode(nonce + key.encrypt(nonce, plaintext.encode("utf-8"), associated_data)).decode()


@dataclass(frozen=True, repr=False)
class _Held:
    value: str
    expires_at: float


class InMemorySecurityContextStore(ISecurityContextStore):
    def __init__(self) -> None:
        self._contexts: dict[str, dict[SecurityParameterKey, _Held]] = {}

    async def save(self, token: str, parameters: Collection[SecurityParameter]) -> None:
        by_key = SecurityParameter.by_key(parameters)
        now = time.monotonic()
        self._contexts[token] = {
            key: _Held(parameter.value, now + parameter.ttl_sec) for key, parameter in by_key.items()
        }

    async def load(self, token: str, keys: Collection[SecurityParameterKey]) -> SecurityContext:
        stored = self._contexts.get(token, {})
        now = time.monotonic()
        return SecurityContext(
            {key: held.value for key in keys if (held := stored.get(key)) is not None and held.expires_at > now}
        )


class NullSecurityContextStore(ISecurityContextStore):
    async def save(self, token: str, parameters: Collection[SecurityParameter]) -> None:
        return None

    async def load(self, token: str, keys: Collection[SecurityParameterKey]) -> SecurityContext:
        return SecurityContext({})


def install_fresh_security_context_store() -> InMemorySecurityContextStore:
    store = InMemorySecurityContextStore()
    security_context.initialize(store)
    return store


def install_fresh_security_context_collector() -> DeclaredParametersCollector:
    collector = DeclaredParametersCollector(declaration_of(headers=("X-Sudir-Token",), cookies=("sudir", "theme")))
    security_context_collector.initialize(collector)
    return collector


def declaration_of(
    *, headers: Iterable[str] = (), cookies: Iterable[str] = (), ttl_sec: int = 600
) -> HeaderCollectorDeclaration:
    return HeaderCollectorDeclaration(
        parameters=(
            *(
                SecurityParameterDeclaration(type=SecurityParameterType.HEADER, name=name, ttl_sec=ttl_sec)
                for name in headers
            ),
            *(
                SecurityParameterDeclaration(type=SecurityParameterType.COOKIE, name=name, ttl_sec=ttl_sec)
                for name in cookies
            ),
        )
    )


def parameters_of(
    *, headers: Mapping[str, str] | None = None, cookies: Mapping[str, str] | None = None, ttl_sec: int = 600
) -> tuple[SecurityParameter, ...]:
    return (
        *(
            SecurityParameter(SecurityParameterKey(SecurityParameterType.HEADER, name), value, ttl_sec)
            for name, value in (headers or {}).items()
        ),
        *(
            SecurityParameter(SecurityParameterKey(SecurityParameterType.COOKIE, name), value, ttl_sec)
            for name, value in (cookies or {}).items()
        ),
    )


def context_of(
    *, headers: Mapping[str, str] | None = None, cookies: Mapping[str, str] | None = None
) -> SecurityContext:
    return SecurityContext(
        {parameter.key: parameter.value for parameter in parameters_of(headers=headers, cookies=cookies)}
    )


class FixedSecurityContextStore(ISecurityContextStore):
    def __init__(
        self,
        contexts: Mapping[str, SecurityContext] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.contexts = dict(contexts or {})
        self.error = error
        self.loads: list[tuple[str, frozenset[SecurityParameterKey]]] = []
        self.saves: list[str] = []

    async def save(self, token: str, parameters: Collection[SecurityParameter]) -> None:
        self.saves.append(token)
        if self.error is not None:
            raise self.error
        self.contexts[token] = SecurityContext(
            {key: parameter.value for key, parameter in SecurityParameter.by_key(parameters).items()}
        )

    async def load(self, token: str, keys: Collection[SecurityParameterKey]) -> SecurityContext:
        self.loads.append((token, frozenset(keys)))
        if self.error is not None:
            raise self.error
        stored = self.contexts.get(token, SecurityContext({}))
        return SecurityContext({key: value for key in keys if (value := stored.value(key)) is not None})


__all__ = [
    "TEST_CHAT_ENCRYPTION_KEY",
    "TEST_CREDENTIALS_KEY",
    "FixedSecurityContextStore",
    "InMemorySecurityContextStore",
    "NullSecurityContextStore",
    "context_of",
    "declaration_of",
    "install_fresh_security_context_collector",
    "install_fresh_security_context_store",
    "parameters_of",
    "sealed_under_the_chat_key",
]
