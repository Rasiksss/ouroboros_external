from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncEngine

from background_memory.infra.repositories._rows import to_message
from new_core.identity import OwnerScope
from ouroboros.storage.runtime_models import ConversationMessage


@dataclass(frozen=True)
class ChatBacklog:
    messages: list[ConversationMessage] = field(default_factory=list)
    anchor_missing: bool = False


class SqlChatHistoryRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def backlog(self, scope: OwnerScope, *, limit: int, after_message_id: str = "") -> ChatBacklog:
        params: dict[str, Any] = {
            "owner_id": scope.owner_id,
            "agent_id": scope.agent_id,
            "chat_id": scope.chat_id,
            "visible": True,
            "limit": max(0, int(limit)),
        }
        async with self._engine.connect() as conn:
            anchor = None
            if after_message_id:
                found = await conn.execute(
                    text(
                        """
                        SELECT created_at, message_id FROM messages
                        WHERE message_id=:anchor
                          AND owner_id=:owner_id AND agent_id=:agent_id AND chat_id=:chat_id
                          AND chat_history_visible=:visible
                        """
                    ),
                    {**params, "anchor": str(after_message_id)},
                )
                anchor = found.mappings().first()
            if anchor is None:
                result = await conn.execute(
                    text(
                        """
                        SELECT * FROM messages
                        WHERE owner_id=:owner_id AND agent_id=:agent_id AND chat_id=:chat_id
                          AND chat_history_visible=:visible
                        ORDER BY created_at ASC, message_id ASC
                        LIMIT :limit
                        """
                    ),
                    params,
                )
                return self._page(result.mappings().all(), anchor_missing=bool(after_message_id))
            params["after_created_at"] = anchor["created_at"]
            params["after_message_id"] = anchor["message_id"]
            result = await conn.execute(
                text(
                    """
                    SELECT * FROM messages
                    WHERE owner_id=:owner_id AND agent_id=:agent_id AND chat_id=:chat_id
                      AND chat_history_visible=:visible
                      AND (created_at > :after_created_at
                           OR (created_at = :after_created_at AND message_id > :after_message_id))
                    ORDER BY created_at ASC, message_id ASC
                    LIMIT :limit
                    """
                ),
                params,
            )
            return self._page(result.mappings().all())

    async def tail_message_id(self, scope: OwnerScope) -> str:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT message_id FROM messages
                    WHERE owner_id=:owner_id AND agent_id=:agent_id AND chat_id=:chat_id
                      AND chat_history_visible=:visible
                    ORDER BY created_at DESC, message_id DESC
                    LIMIT 1
                    """
                ),
                {
                    "owner_id": scope.owner_id,
                    "agent_id": scope.agent_id,
                    "chat_id": scope.chat_id,
                    "visible": True,
                },
            )
            tail = result.scalar_one_or_none()
        return str(tail or "")

    async def message_exists(self, message_id: str) -> bool:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM messages WHERE message_id=:message_id"),
                {"message_id": str(message_id)},
            )
            found = result.first()
        return found is not None

    def _page(self, rows: Sequence[RowMapping], *, anchor_missing: bool = False) -> ChatBacklog:
        return ChatBacklog(messages=[to_message(row) for row in rows], anchor_missing=anchor_missing)


__all__ = ["ChatBacklog", "SqlChatHistoryRepo"]
