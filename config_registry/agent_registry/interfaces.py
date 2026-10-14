from __future__ import annotations

from abc import ABC, abstractmethod

from config_registry.agent_registry.domain import AgentEntity


class IAgentRegistryRepo(ABC):
    @abstractmethod
    async def get_or_create_default_agent(self, owner_id: str) -> AgentEntity:
        pass

    @abstractmethod
    async def create_agent(self, owner_id: str, name: str, description: str) -> AgentEntity:
        pass

    @abstractmethod
    async def get_agent(self, owner_id: str, agent_id: str) -> AgentEntity | None:
        pass

    @abstractmethod
    async def list_agents(self, owner_id: str) -> list[AgentEntity]:
        pass

    @abstractmethod
    async def deactivate_agent(self, owner_id: str, agent_id: str) -> bool:
        pass


__all__ = ["IAgentRegistryRepo"]
