from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from config_registry.agent_registry.domain import AgentEntity
from config_registry.agent_registry.infra.repositories.repo import SqlAgentRegistryRepo
from config_registry.agent_registry.interfaces import IAgentRegistryRepo
from infra.async_engine import async_engine_factory
from infra.utils.http.fastapi import resolve_owner

router = APIRouter(tags=["agents_registry"])


def get_registry() -> IAgentRegistryRepo:
    return SqlAgentRegistryRepo(async_engine_factory.engine)


class AgentPayload(BaseModel):
    agent_id: str
    owner_id: str
    name: str
    description: str
    is_active: bool

    @classmethod
    def build(cls, agent: AgentEntity) -> AgentPayload:
        return cls(
            agent_id=agent.agent_id,
            owner_id=agent.owner_id,
            name=agent.name,
            description=agent.description,
            is_active=agent.is_active,
        )


class CreateAgentRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""


@router.get("/", response_model=list[AgentPayload], summary="List this owner's agents")
async def list_agents(
    owner_id: str = Depends(resolve_owner), registry: IAgentRegistryRepo = Depends(get_registry)
) -> list[AgentPayload]:
    return [AgentPayload.build(agent) for agent in await registry.list_agents(owner_id)]


@router.post(
    "/",
    response_model=AgentPayload,
    status_code=status.HTTP_201_CREATED,
    summary="Create a sub-agent for this owner",
)
async def create_agent(
    body: CreateAgentRequest,
    owner_id: str = Depends(resolve_owner),
    registry: IAgentRegistryRepo = Depends(get_registry),
) -> AgentPayload:
    return AgentPayload.build(await registry.create_agent(owner_id, body.name, body.description))


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Deactivate one of this owner's agents")
async def deactivate_agent(
    agent_id: str,
    owner_id: str = Depends(resolve_owner),
    registry: IAgentRegistryRepo = Depends(get_registry),
) -> None:
    if not await registry.deactivate_agent(owner_id, agent_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such agent")


__all__ = ["AgentPayload", "CreateAgentRequest", "get_registry", "router"]
