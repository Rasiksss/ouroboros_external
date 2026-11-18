from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from pydantic import BaseModel

from ouroboros.storage.settings.schema import A2aRemoteAgentEntry, EgressAuthKind, UserTokenAuth


class A2aAgentPayload(BaseModel):
    id: str
    name: str
    auth_kind: EgressAuthKind
    personal_token_supported: bool
    personal_token_writable: bool
    has_static_token: bool
    personal_token_expires_at: datetime | None


class A2aAgentsPayload(BaseModel):
    agents: list[A2aAgentPayload]


class A2aPersonalTokenPayload(BaseModel):
    remote_agent_id: str
    expires_at: datetime


class A2aPersonalTokenClearedPayload(BaseModel):
    remote_agent_id: str
    cleared: bool


def a2a_agents_payload(
    agents: Sequence[A2aRemoteAgentEntry],
    *,
    writable: bool,
    expirations: Mapping[str, datetime],
) -> A2aAgentsPayload:
    return A2aAgentsPayload(
        agents=[
            A2aAgentPayload(
                id=entry.id,
                name=entry.name,
                auth_kind=entry.auth.kind,
                personal_token_supported=entry.takes_personal_token,
                personal_token_writable=writable and entry.takes_personal_token,
                has_static_token=isinstance(entry.auth, UserTokenAuth) and entry.auth.auth_token is not None,
                personal_token_expires_at=expirations.get(entry.id) if entry.takes_personal_token else None,
            )
            for entry in agents
        ]
    )


__all__ = [
    "A2aAgentPayload",
    "A2aAgentsPayload",
    "A2aPersonalTokenClearedPayload",
    "A2aPersonalTokenPayload",
    "a2a_agents_payload",
]
