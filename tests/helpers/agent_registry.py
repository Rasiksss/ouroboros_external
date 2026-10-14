from __future__ import annotations

from config_registry.agent_registry.domain import AgentEntity
from config_registry.agent_registry.interfaces import IAgentRegistryRepo
from ouroboros.contracts.owner_scope import SINGLE_AGENT_ID


class FakeAgentRegistry(IAgentRegistryRepo):
    def __init__(self, *, default_agent_id: str = SINGLE_AGENT_ID) -> None:
        self.by_owner: dict[str, list[AgentEntity]] = {}
        self.default_agent_id = default_agent_id
        self.created_for: list[str] = []
        self._next_id = 0

    async def get_or_create_default_agent(self, owner_id: str) -> AgentEntity:
        for agent in self.by_owner.get(owner_id, []):
            if agent.agent_id == self.default_agent_id:
                return agent
        agent = AgentEntity(
            agent_id=self.default_agent_id,
            owner_id=owner_id,
            name="main",
            description="",
            is_active=True,
        )
        self.by_owner.setdefault(owner_id, []).append(agent)
        self.created_for.append(owner_id)
        return agent

    async def create_agent(self, owner_id: str, name: str, description: str) -> AgentEntity:
        self._next_id += 1
        agent = AgentEntity(
            agent_id=f"sub-{self._next_id}",
            owner_id=owner_id,
            name=name,
            description=description,
            is_active=True,
        )
        self.by_owner.setdefault(owner_id, []).append(agent)
        return agent

    async def get_agent(self, owner_id: str, agent_id: str) -> AgentEntity | None:
        return next((a for a in self.by_owner.get(owner_id, []) if a.agent_id == agent_id), None)

    async def list_agents(self, owner_id: str) -> list[AgentEntity]:
        return list(self.by_owner.get(owner_id, []))

    async def deactivate_agent(self, owner_id: str, agent_id: str) -> bool:
        agents = self.by_owner.get(owner_id, [])
        for index, agent in enumerate(agents):
            if agent.agent_id == agent_id:
                agents[index] = AgentEntity(
                    agent_id=agent.agent_id,
                    owner_id=agent.owner_id,
                    name=agent.name,
                    description=agent.description,
                    is_active=False,
                )
                return True
        return False


class UnreachableAgentRegistry(IAgentRegistryRepo):
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or OSError("the runtime database is unreachable")

    async def get_or_create_default_agent(self, owner_id: str) -> AgentEntity:
        raise self._error

    async def create_agent(self, owner_id: str, name: str, description: str) -> AgentEntity:
        raise self._error

    async def get_agent(self, owner_id: str, agent_id: str) -> AgentEntity | None:
        raise self._error

    async def list_agents(self, owner_id: str) -> list[AgentEntity]:
        raise self._error

    async def deactivate_agent(self, owner_id: str, agent_id: str) -> bool:
        raise self._error


__all__ = ["FakeAgentRegistry", "UnreachableAgentRegistry"]
