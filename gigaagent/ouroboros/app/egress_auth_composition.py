from __future__ import annotations

from collections.abc import Iterator, Sequence
from enum import StrEnum

from pydantic import SecretStr

from infra.async_engine import AsyncEngineFactory
from infra.security_context.declaration import HeaderCollectorDeclaration
from infra.security_context.interface import SecurityParameterKey, SecurityParameterType
from infra.user_tokens.interface import IUserTokenStore
from infra.user_tokens.sql import SqlUserTokenStore
from ouroboros.storage.settings.schema import EgressEntry, UserHeadersAuth


class EgressAuthCompositionError(RuntimeError):
    pass


class EgressList(StrEnum):
    MCP = "security_config/mcp.py"
    A2A = "security_config/a2a.py"


_LIST_ENTRIES = {EgressList.MCP: "MCP servers", EgressList.A2A: "A2A agents"}


class EgressAuthComposition:
    @classmethod
    def personal_token_targets(cls, entries: Sequence[EgressEntry]) -> list[str]:
        return [entry.id for entry in entries if entry.enabled and entry.takes_personal_token]

    @classmethod
    def user_token_store(
        cls, entries: Sequence[EgressEntry], *, engine_factory: AsyncEngineFactory, encryption_key: SecretStr
    ) -> IUserTokenStore:
        try:
            return SqlUserTokenStore(engine_factory.engine, encryption_key=encryption_key)
        except ValueError as exc:
            target_ids = ", ".join(cls.personal_token_targets(entries)) or "none"
            raise EgressAuthCompositionError(
                "personal MCP and A2A tokens are sealed in the runtime database; set "
                "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY to 64 bytes of base64 (MCP user_token servers over "
                "streamable_http and A2A user_token agents: "
                f"{target_ids})"
            ) from exc

    @classmethod
    def undeclared_user_headers(
        cls, entries: Sequence[EgressEntry], declaration: HeaderCollectorDeclaration
    ) -> list[str]:
        declared = frozenset(parameter.key for parameter in declaration.parameters)
        return [
            f"{entry.id} {key.label}"
            for entry in entries
            if entry.enabled and isinstance(entry.auth, UserHeadersAuth)
            for key in cls._forwarded_keys(entry.auth)
            if key not in declared
        ]

    @classmethod
    def undeclared_user_headers_refusal(
        cls, entries: Sequence[EgressEntry], declaration: HeaderCollectorDeclaration, *, source: EgressList
    ) -> str | None:
        undeclared = cls.undeclared_user_headers(entries, declaration)
        if not undeclared:
            return None
        return (
            f"{_LIST_ENTRIES[source]} forward user credentials the header collector does not declare: "
            f"{', '.join(undeclared)}; no call to them could carry these values, so declare each in "
            f"security_config/auth/header_collector.yaml or stop forwarding it in {source}"
        )

    @classmethod
    def refuse_undeclared_user_headers(
        cls, entries: Sequence[EgressEntry], declaration: HeaderCollectorDeclaration, *, source: EgressList
    ) -> None:
        refusal = cls.undeclared_user_headers_refusal(entries, declaration, source=source)
        if refusal is not None:
            raise EgressAuthCompositionError(refusal)

    @classmethod
    def _forwarded_keys(cls, auth: UserHeadersAuth) -> Iterator[SecurityParameterKey]:
        for name in auth.headers:
            yield SecurityParameterKey(SecurityParameterType.HEADER, name)
        for name in auth.cookies:
            yield SecurityParameterKey(SecurityParameterType.COOKIE, name)


__all__ = ["EgressAuthComposition", "EgressAuthCompositionError", "EgressList"]
