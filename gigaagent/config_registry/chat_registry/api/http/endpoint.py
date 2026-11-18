from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from config_registry.agent_registry.infra.repositories.repo import SqlAgentRegistryRepo
from config_registry.agent_registry.interfaces import IAgentRegistryRepo
from config_registry.chat_registry.domain import ChatEntity, TitleSource, chat_title
from config_registry.chat_registry.infra.repositories.repo import SqlChatRegistryRepo
from config_registry.chat_registry.interfaces import IChatRegistryRepo
from infra.async_engine import async_engine_factory
from infra.utils.http.fastapi import resolve_owner
from ouroboros.contracts.chat_id_policy import is_internal_chat_id

router = APIRouter(tags=["chat_registry"])


def get_chats() -> IChatRegistryRepo:
    return SqlChatRegistryRepo(async_engine_factory.engine)


def get_registry() -> IAgentRegistryRepo:
    return SqlAgentRegistryRepo(async_engine_factory.engine)


class ChatPayload(BaseModel):
    owner_id: str
    agent_id: str
    chat_id: str
    title: str
    title_source: TitleSource
    is_active: bool
    created_at: str

    @classmethod
    def of(cls, chat: ChatEntity) -> ChatPayload:
        return cls(
            owner_id=chat.owner_id,
            agent_id=chat.agent_id,
            chat_id=chat.chat_id,
            title=chat.title,
            title_source=chat.title_source,
            is_active=chat.is_active,
            created_at=chat.created_at,
        )


class UpdateChatRequest(BaseModel):
    chat_id: str
    title: str | None = None
    is_active: bool | None = None


async def _require_agent(owner_id: str, agent_id: str, agents: IAgentRegistryRepo) -> None:
    if await agents.get_agent(owner_id, agent_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such agent")


def _require_addressable(chat_id: str) -> str:
    clean = str(chat_id or "").strip()
    if not clean or is_internal_chat_id(clean):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unusable chat id")
    return clean


@router.get("/{agent_id}", response_model=list[ChatPayload], summary="List this owner's conversations")
async def list_chats(
    agent_id: str,
    include_retired: bool = False,
    owner_id: str = Depends(resolve_owner),
    chats: IChatRegistryRepo = Depends(get_chats),
    agents: IAgentRegistryRepo = Depends(get_registry),
) -> list[ChatPayload]:
    await _require_agent(owner_id, agent_id, agents)
    return [
        ChatPayload.of(chat) for chat in await chats.list_chats(owner_id, agent_id, include_retired=include_retired)
    ]


@router.patch("/{agent_id}", response_model=ChatPayload, summary="Rename, retire or restore one conversation")
async def update_chat(
    agent_id: str,
    body: UpdateChatRequest,
    owner_id: str = Depends(resolve_owner),
    chats: IChatRegistryRepo = Depends(get_chats),
    agents: IAgentRegistryRepo = Depends(get_registry),
) -> ChatPayload:
    chat_id = _require_addressable(body.chat_id)
    title = chat_title(body.title) if body.title is not None else None
    if body.title is not None and not title:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="a title may not be empty")
    await _require_agent(owner_id, agent_id, agents)
    if await chats.get_chat(owner_id, agent_id, chat_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such chat")
    if title is not None:
        await chats.rename_chat(owner_id, agent_id, chat_id, title=title)
    if body.is_active is not None:
        await chats.set_chat_active(owner_id, agent_id, chat_id, is_active=body.is_active)
    updated = await chats.get_chat(owner_id, agent_id, chat_id)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such chat")
    return ChatPayload.of(updated)


__all__ = ["ChatPayload", "UpdateChatRequest", "get_chats", "get_registry", "router"]
