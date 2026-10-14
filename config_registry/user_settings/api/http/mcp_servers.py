from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from ouroboros.storage.settings.schema import EgressAuthKind, McpServerEntry, UserTokenAuth


class McpServerPayload(BaseModel):
    id: str
    name: str
    transport: Literal["sse", "streamable_http"]
    auth_kind: EgressAuthKind
    selected: bool
    personal_token_supported: bool
    personal_token_writable: bool
    has_static_token: bool
    personal_token_expires_at: datetime | None


class McpServersPayload(BaseModel):
    servers: list[McpServerPayload]


class PersonalTokenPayload(BaseModel):
    server_id: str
    expires_at: datetime


class PersonalTokenClearedPayload(BaseModel):
    server_id: str
    cleared: bool


def mcp_servers_payload(
    servers: Sequence[McpServerEntry],
    *,
    selected: Collection[str],
    supported: Collection[str],
    writable: bool,
    expirations: Mapping[str, datetime],
) -> McpServersPayload:
    return McpServersPayload(
        servers=[
            McpServerPayload(
                id=entry.id,
                name=entry.name,
                transport=entry.transport,
                auth_kind=entry.auth.kind,
                selected=entry.id in selected,
                personal_token_supported=entry.id in supported,
                personal_token_writable=writable and entry.id in supported,
                has_static_token=isinstance(entry.auth, UserTokenAuth) and entry.auth.auth_token is not None,
                personal_token_expires_at=expirations.get(entry.id) if entry.id in supported else None,
            )
            for entry in servers
        ]
    )


__all__ = [
    "McpServerPayload",
    "McpServersPayload",
    "PersonalTokenClearedPayload",
    "PersonalTokenPayload",
    "mcp_servers_payload",
]
