from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentEntity:
    agent_id: str
    owner_id: str
    name: str
    description: str
    is_active: bool


__all__ = ["AgentEntity"]
