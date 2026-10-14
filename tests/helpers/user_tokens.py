from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from pydantic import SecretStr
from sqlalchemy import create_engine, text

from infra.credentials_key import CredentialsKey
from infra.user_tokens.interface import IUserTokenRepo, IUserTokenStore, UserTokenTarget, UserTokenTargetKind
from infra.user_tokens.sql import SqlUserTokenStore

_TABLES = {
    UserTokenTargetKind.MCP_SERVER: ("mcp_user_tokens", "mcp_server_id"),
    UserTokenTargetKind.A2A_AGENT: ("a2a_user_tokens", "a2a_agent_id"),
}


class FixedUserTokenStore(IUserTokenStore):
    def __init__(
        self, tokens: Mapping[tuple[UserTokenTarget, str, str], str], *, error: Exception | None = None
    ) -> None:
        self.tokens = dict(tokens)
        self.error = error
        self.loads: list[tuple[UserTokenTarget, str, str]] = []

    async def load(self, target: UserTokenTarget, owner_id: str, agent_id: str) -> str | None:
        self.loads.append((target, owner_id, agent_id))
        if self.error is not None:
            raise self.error
        return self.tokens.get((target, owner_id, agent_id))


class NullUserTokenStore(IUserTokenStore):
    async def load(self, target: UserTokenTarget, owner_id: str, agent_id: str) -> str | None:
        return None


class UnreadUserTokenStore(IUserTokenStore):
    async def load(self, target: UserTokenTarget, owner_id: str, agent_id: str) -> str | None:
        raise AssertionError(f"the user token store was read for {target.kind.value} {target.target_id}")


def unread_user_token_store() -> UnreadUserTokenStore:
    return UnreadUserTokenStore()


class FakeUserTokenRepo(IUserTokenRepo):
    def __init__(self) -> None:
        self.rows: dict[tuple[UserTokenTarget, str, str], tuple[str, datetime]] = {}

    async def save(
        self, target: UserTokenTarget, owner_id: str, agent_id: str, token: str, expires_at: datetime
    ) -> None:
        self.rows[(target, owner_id, agent_id)] = (token, expires_at)

    async def clear(self, target: UserTokenTarget, owner_id: str, agent_id: str) -> bool:
        return self.rows.pop((target, owner_id, agent_id), None) is not None

    async def expirations(self, kind: UserTokenTargetKind, owner_id: str, agent_id: str) -> dict[str, datetime]:
        now = datetime.now(UTC)
        return {
            target.target_id: expires_at
            for (target, owner, agent), (_, expires_at) in self.rows.items()
            if (target.kind, owner, agent) == (kind, owner_id, agent_id) and expires_at > now
        }


def seed_user_token(
    db_url: str,
    key: SecretStr,
    *,
    target: UserTokenTarget,
    owner_id: str,
    agent_id: str,
    token: str,
    expires_at: datetime,
) -> str:
    credentials_key = CredentialsKey(key)
    table, id_column = _TABLES[target.kind]
    row_id = uuid.uuid4().hex
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {table} (id, {id_column}, agent_id, owner_id, token, expires_at) "
                    "VALUES (:id, :target_id, :agent_id, :owner_id, :token, :expires_at)"
                ),
                {
                    "id": row_id,
                    "target_id": target.target_id,
                    "agent_id": agent_id,
                    "owner_id": owner_id,
                    "token": credentials_key.seal(
                        token, associated_data=SqlUserTokenStore._associated_data(target, owner_id, agent_id)
                    ),
                    "expires_at": SqlUserTokenStore._timestamp(expires_at),
                },
            )
    finally:
        engine.dispose()
    return row_id


__all__ = [
    "FakeUserTokenRepo",
    "FixedUserTokenStore",
    "NullUserTokenStore",
    "UnreadUserTokenStore",
    "seed_user_token",
    "unread_user_token_store",
]
