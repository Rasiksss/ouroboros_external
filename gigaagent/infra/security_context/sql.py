from __future__ import annotations

import json
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine

from infra.credentials_key import CredentialsKey
from infra.security_context.interface import (
    ISecurityContextStore,
    SecurityContext,
    SecurityParameter,
    SecurityParameterKey,
)


class SqlSecurityContextStore(ISecurityContextStore):
    def __init__(self, engine: AsyncEngine, *, encryption_key: SecretStr) -> None:
        self._key = CredentialsKey(encryption_key)
        self._engine = engine

    async def save(self, token: str, parameters: Collection[SecurityParameter]) -> None:
        by_key = SecurityParameter.by_key(parameters)
        now = datetime.now(UTC)
        rows = [
            {
                "security_token": token,
                "param_type": key.type.value,
                "param_name": key.name,
                "param_value": self._key.seal(
                    parameter.value, associated_data=self._associated_data(token, key.type.value, key.name)
                ),
                "expires_at": self._timestamp(now + timedelta(seconds=parameter.ttl_sec)),
            }
            for key, parameter in sorted(by_key.items(), key=lambda item: item[0].label)
        ]
        async with self._engine.begin() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('security-contexts'), hashtext(:token))"),
                {"token": token},
            )
            if not rows:
                await conn.execute(
                    text("DELETE FROM security_contexts WHERE security_token = :token"),
                    {"token": token},
                )
                return
            await conn.execute(
                text(
                    "INSERT INTO security_contexts "
                    "(security_token, param_type, param_name, param_value, expires_at) "
                    "VALUES (:security_token, :param_type, :param_name, :param_value, :expires_at) "
                    "ON CONFLICT (security_token, param_type, param_name) "
                    "DO UPDATE SET param_value = excluded.param_value, expires_at = excluded.expires_at"
                ),
                rows,
            )
            await conn.execute(
                text(
                    "DELETE FROM security_contexts WHERE security_token = :token "
                    "AND (param_type, param_name) NOT IN :keys"
                ).bindparams(bindparam("keys", expanding=True)),
                {"token": token, "keys": [(row["param_type"], row["param_name"]) for row in rows]},
            )

    async def load(self, token: str, keys: Collection[SecurityParameterKey]) -> SecurityContext:
        requested = {(key.type.value, key.name): key for key in keys}
        if not requested:
            return SecurityContext({})
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT param_type, param_name, param_value FROM security_contexts "
                        "WHERE security_token = :token AND expires_at > :now "
                        "AND (param_type, param_name) IN :keys"
                    ).bindparams(bindparam("keys", expanding=True)),
                    {"token": token, "now": self._timestamp(datetime.now(UTC)), "keys": list(requested)},
                )
            ).all()
        return SecurityContext(
            {
                requested[(str(param_type), str(param_name))]: self._key.unseal(
                    str(param_value), associated_data=self._associated_data(token, str(param_type), str(param_name))
                )
                for param_type, param_name, param_value in rows
            }
        )

    @staticmethod
    def _associated_data(token: str, param_type: str, param_name: str) -> bytes:
        return json.dumps([token, param_type, param_name]).encode("utf-8")

    @staticmethod
    def _timestamp(moment: datetime) -> str:
        if moment.tzinfo is None:
            raise ValueError("a security context timestamp must carry a timezone")
        return moment.astimezone(UTC).isoformat(timespec="microseconds")


__all__ = ["SqlSecurityContextStore"]
