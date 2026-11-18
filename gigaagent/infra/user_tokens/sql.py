from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import assert_never

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from infra.credentials_key import CredentialsKey
from infra.user_tokens.interface import IUserTokenRepo, IUserTokenStore, UserTokenTarget, UserTokenTargetKind

_TABLES = {
    UserTokenTargetKind.MCP_SERVER: ("mcp_user_tokens", "mcp_server_id"),
    UserTokenTargetKind.A2A_AGENT: ("a2a_user_tokens", "a2a_agent_id"),
}


class SqlUserTokenStore(IUserTokenStore):
    def __init__(self, engine: AsyncEngine, *, encryption_key: SecretStr) -> None:
        self._key = CredentialsKey(encryption_key)
        self._engine = engine

    async def load(self, target: UserTokenTarget, owner_id: str, agent_id: str) -> str | None:
        table, id_column = _TABLES[target.kind]
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"SELECT token FROM {table} "
                    f"WHERE {id_column} = :target_id AND owner_id = :owner_id AND agent_id = :agent_id "
                    "AND expires_at > :now"
                ),
                {
                    "target_id": target.target_id,
                    "owner_id": owner_id,
                    "agent_id": agent_id,
                    "now": self._timestamp(datetime.now(UTC)),
                },
            )
            sealed = result.scalar_one_or_none()
        if sealed is None:
            return None
        return self._key.unseal(str(sealed), associated_data=self._associated_data(target, owner_id, agent_id))

    @classmethod
    def _associated_data(cls, target: UserTokenTarget, owner_id: str, agent_id: str) -> bytes:
        match target.kind:
            case UserTokenTargetKind.MCP_SERVER:
                fields = [target.target_id, owner_id, agent_id]
            case UserTokenTargetKind.A2A_AGENT:
                fields = ["a2a-agent", target.target_id, owner_id, agent_id]
            case _:
                assert_never(target.kind)
        return json.dumps(fields).encode("utf-8")

    @classmethod
    def _timestamp(cls, moment: datetime) -> str:
        if moment.tzinfo is None:
            raise ValueError("a user token timestamp must carry a timezone")
        return moment.astimezone(UTC).isoformat(timespec="microseconds")


class SqlUserTokenRepo(IUserTokenRepo):
    def __init__(self, engine: AsyncEngine, *, encryption_key: SecretStr) -> None:
        self._key = CredentialsKey(encryption_key)
        self._engine = engine

    async def save(
        self, target: UserTokenTarget, owner_id: str, agent_id: str, token: str, expires_at: datetime
    ) -> None:
        row = {
            "id": uuid.uuid4().hex,
            "target_id": target.target_id,
            "owner_id": owner_id,
            "agent_id": agent_id,
            "expires_at": SqlUserTokenStore._timestamp(expires_at),
            "token": self._key.seal(
                token, associated_data=SqlUserTokenStore._associated_data(target, owner_id, agent_id)
            ),
        }
        table, id_column = _TABLES[target.kind]
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    f"INSERT INTO {table} (id, {id_column}, owner_id, agent_id, token, expires_at) "
                    "VALUES (:id, :target_id, :owner_id, :agent_id, :token, :expires_at) "
                    f"ON CONFLICT({id_column}, owner_id, agent_id) "
                    "DO UPDATE SET token = excluded.token, expires_at = excluded.expires_at"
                ),
                row,
            )

    async def clear(self, target: UserTokenTarget, owner_id: str, agent_id: str) -> bool:
        table, id_column = _TABLES[target.kind]
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    f"DELETE FROM {table} "
                    f"WHERE {id_column} = :target_id AND owner_id = :owner_id AND agent_id = :agent_id"
                ),
                {"target_id": target.target_id, "owner_id": owner_id, "agent_id": agent_id},
            )
            cleared = result.rowcount
        return bool(cleared)

    async def expirations(self, kind: UserTokenTargetKind, owner_id: str, agent_id: str) -> dict[str, datetime]:
        table, id_column = _TABLES[kind]
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"SELECT {id_column}, expires_at FROM {table} "
                    "WHERE owner_id = :owner_id AND agent_id = :agent_id AND expires_at > :now"
                ),
                {
                    "owner_id": owner_id,
                    "agent_id": agent_id,
                    "now": SqlUserTokenStore._timestamp(datetime.now(UTC)),
                },
            )
            rows = result.all()
        return {str(target_id): datetime.fromisoformat(str(expires_at)) for target_id, expires_at in rows}


__all__ = ["SqlUserTokenRepo", "SqlUserTokenStore"]
