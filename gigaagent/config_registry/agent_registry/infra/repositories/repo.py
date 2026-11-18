from __future__ import annotations

import uuid

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncEngine

from config_registry.agent_registry.domain import AgentEntity
from config_registry.agent_registry.interfaces import IAgentRegistryRepo
from ouroboros.contracts.owner_scope import SINGLE_AGENT_ID
from ouroboros.utils import utc_now_iso


def _entity(row: RowMapping) -> AgentEntity:
    return AgentEntity(
        agent_id=str(row["agent_id"]),
        owner_id=str(row["owner_id"]),
        name=str(row["name"] or ""),
        description=str(row["description"] or ""),
        is_active=bool(row["is_active"]),
    )


class SqlAgentRegistryRepo(IAgentRegistryRepo):
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get_or_create_default_agent(self, owner_id: str) -> AgentEntity:
        keys = {"owner_id": owner_id, "agent_id": SINGLE_AGENT_ID}
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO agents (owner_id, agent_id, name, description, is_active, created_at) "
                    "VALUES (:owner_id, :agent_id, :name, :description, :is_active, :created_at) "
                    "ON CONFLICT(owner_id, agent_id) DO NOTHING"
                ),
                {
                    **keys,
                    "name": "main",
                    "description": "",
                    "is_active": True,
                    "created_at": utc_now_iso(),
                },
            )
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT owner_id, agent_id, name, description, is_active FROM agents "
                            "WHERE owner_id = :owner_id AND agent_id = :agent_id"
                        ),
                        keys,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise RuntimeError("the default agent is absent immediately after being written")
        return _entity(row)

    async def create_agent(self, owner_id: str, name: str, description: str) -> AgentEntity:
        agent_id = uuid.uuid4().hex
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO agents (owner_id, agent_id, name, description, is_active, created_at) "
                    "VALUES (:owner_id, :agent_id, :name, :description, :is_active, :created_at)"
                ),
                {
                    "owner_id": owner_id,
                    "agent_id": agent_id,
                    "name": name,
                    "description": description,
                    "is_active": True,
                    "created_at": utc_now_iso(),
                },
            )
        return AgentEntity(
            agent_id=agent_id,
            owner_id=owner_id,
            name=name,
            description=description,
            is_active=True,
        )

    async def get_agent(self, owner_id: str, agent_id: str) -> AgentEntity | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT owner_id, agent_id, name, description, is_active FROM agents "
                            "WHERE owner_id = :owner_id AND agent_id = :agent_id"
                        ),
                        {"owner_id": owner_id, "agent_id": agent_id},
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _entity(row)

    async def list_agents(self, owner_id: str) -> list[AgentEntity]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT owner_id, agent_id, name, description, is_active FROM agents "
                            "WHERE owner_id = :owner_id ORDER BY created_at, agent_id"
                        ),
                        {"owner_id": owner_id},
                    )
                )
                .mappings()
                .all()
            )
        return [_entity(row) for row in rows]

    async def deactivate_agent(self, owner_id: str, agent_id: str) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("UPDATE agents SET is_active = :inactive WHERE owner_id = :owner_id AND agent_id = :agent_id"),
                {"owner_id": owner_id, "agent_id": agent_id, "inactive": False},
            )
        return result.rowcount == 1


__all__ = ["SqlAgentRegistryRepo"]
