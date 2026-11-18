from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from config_registry.chat_registry.domain import ChatEntity, TitleSource
from config_registry.chat_registry.interfaces import ChatRegistryStore, IChatRegistryRepo
from new_core.identity import OwnerScope
from ouroboros.storage.message_field_crypto import decrypt_field, encrypt_field
from ouroboros.storage.runtime_engine import resolve_runtime_engine
from ouroboros.utils import utc_now_iso


def _entity(row: Any) -> ChatEntity:
    return ChatEntity(
        owner_id=str(row["owner_id"]),
        agent_id=str(row["agent_id"]),
        chat_id=str(row["chat_id"]),
        title=decrypt_field(str(row["title"] or "")),
        title_source=TitleSource(str(row["title_source"])),
        title_attempts=int(row["title_attempts"] or 0),
        is_active=bool(row["is_active"]),
        created_at=str(row["created_at"] or ""),
    )


def _keys(scope: OwnerScope) -> dict[str, str]:
    return {"owner_id": scope.owner_id, "agent_id": scope.agent_id, "chat_id": scope.chat_id}


class SqlChatRegistryStore(ChatRegistryStore):
    def ensure_chat(self, scope: OwnerScope, *, provisional_title: str) -> ChatEntity:
        with resolve_runtime_engine("chat registry").begin() as conn:
            return self.ensure_chat_in_transaction(conn, scope, provisional_title=provisional_title)

    def ensure_chat_in_transaction(self, conn: Connection, scope: OwnerScope, *, provisional_title: str) -> ChatEntity:
        keys = _keys(scope)
        conn.execute(
            text(
                "INSERT INTO chats (owner_id, agent_id, chat_id, title, title_source, title_attempts, is_active, created_at) VALUES (:owner_id, :agent_id, :chat_id, :title, "
                ":title_source, :title_attempts, :is_active, :created_at) "
                "ON CONFLICT (owner_id, agent_id, chat_id) DO NOTHING"
            ),
            {
                **keys,
                "title": encrypt_field(provisional_title),
                "title_source": TitleSource.PROVISIONAL.value,
                "title_attempts": 0,
                "is_active": True,
                "created_at": utc_now_iso(),
            },
        )
        row = (
            conn.execute(
                text(
                    "SELECT owner_id, agent_id, chat_id, title, title_source, title_attempts, is_active, created_at FROM chats "
                    "WHERE owner_id = :owner_id AND agent_id = :agent_id AND chat_id = :chat_id"
                ),
                keys,
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RuntimeError("the conversation is absent immediately after being recorded")
        return _entity(row)

    def get_chat(self, scope: OwnerScope) -> ChatEntity | None:
        engine = resolve_runtime_engine("chat registry")
        with engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT owner_id, agent_id, chat_id, title, title_source, title_attempts, is_active, created_at FROM chats "
                        "WHERE owner_id = :owner_id AND agent_id = :agent_id AND chat_id = :chat_id"
                    ),
                    _keys(scope),
                )
                .mappings()
                .first()
            )
        return None if row is None else _entity(row)

    def claim_title_attempt(self, scope: OwnerScope, *, max_attempts: int) -> bool:
        with resolve_runtime_engine("chat registry").begin() as conn:
            claimed = conn.execute(
                text(
                    "UPDATE chats SET title_attempts = title_attempts + 1 "
                    "WHERE owner_id = :owner_id AND agent_id = :agent_id AND chat_id = :chat_id "
                    "AND title_source = :provisional AND title_attempts < :max_attempts"
                ),
                {
                    **_keys(scope),
                    "provisional": TitleSource.PROVISIONAL.value,
                    "max_attempts": int(max_attempts),
                },
            ).rowcount
        return bool(claimed)

    def store_generated_title(self, scope: OwnerScope, *, title: str) -> bool:
        engine = resolve_runtime_engine("chat registry")
        with engine.begin() as conn:
            written = conn.execute(
                text(
                    "UPDATE chats SET title = :title, title_source = :model "
                    "WHERE owner_id = :owner_id AND agent_id = :agent_id AND chat_id = :chat_id "
                    "AND title_source = :provisional"
                ),
                {
                    **_keys(scope),
                    "title": encrypt_field(title),
                    "model": TitleSource.MODEL.value,
                    "provisional": TitleSource.PROVISIONAL.value,
                },
            ).rowcount
        return bool(written)

    def delete_owner_chats(self, *, owner_id: str, agent_id: str) -> int:
        with resolve_runtime_engine("chat registry").begin() as conn:
            removed = conn.execute(
                text("DELETE FROM chats WHERE owner_id = :owner_id AND agent_id = :agent_id"),
                {"owner_id": str(owner_id), "agent_id": str(agent_id)},
            ).rowcount
        return int(removed or 0)


class SqlChatRegistryRepo(IChatRegistryRepo):
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_chats(self, owner_id: str, agent_id: str, *, include_retired: bool = False) -> list[ChatEntity]:
        # The registry's own creation order, newest first: the record carries no last-activity
        # value, and `chat_id` is the tie-break because ids are opaque strings a client cannot
        # order itself.
        active_clause = "" if include_retired else " AND is_active = :active"
        params: dict[str, Any] = {"owner_id": owner_id, "agent_id": agent_id}
        if not include_retired:
            params["active"] = True
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT owner_id, agent_id, chat_id, title, title_source, title_attempts, is_active, created_at FROM chats "
                            f"WHERE owner_id = :owner_id AND agent_id = :agent_id{active_clause} "
                            "ORDER BY created_at DESC, chat_id DESC"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        return [_entity(row) for row in rows]

    async def get_chat(self, owner_id: str, agent_id: str, chat_id: str) -> ChatEntity | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT owner_id, agent_id, chat_id, title, title_source, title_attempts, is_active, created_at FROM chats "
                            "WHERE owner_id = :owner_id AND agent_id = :agent_id AND chat_id = :chat_id"
                        ),
                        {"owner_id": owner_id, "agent_id": agent_id, "chat_id": chat_id},
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _entity(row)

    async def claim_title_attempt(self, owner_id: str, agent_id: str, chat_id: str, *, max_attempts: int) -> bool:
        async with self._engine.begin() as conn:
            claimed = (
                await conn.execute(
                    text(
                        "UPDATE chats SET title_attempts = title_attempts + 1 "
                        "WHERE owner_id = :owner_id AND agent_id = :agent_id AND chat_id = :chat_id "
                        "AND title_source = :provisional AND title_attempts < :max_attempts"
                    ),
                    {
                        "owner_id": owner_id,
                        "agent_id": agent_id,
                        "chat_id": chat_id,
                        "provisional": TitleSource.PROVISIONAL.value,
                        "max_attempts": int(max_attempts),
                    },
                )
            ).rowcount
        return bool(claimed)

    async def store_generated_title(self, owner_id: str, agent_id: str, chat_id: str, *, title: str) -> bool:
        async with self._engine.begin() as conn:
            written = (
                await conn.execute(
                    text(
                        "UPDATE chats SET title = :title, title_source = :model "
                        "WHERE owner_id = :owner_id AND agent_id = :agent_id AND chat_id = :chat_id "
                        "AND title_source = :provisional"
                    ),
                    {
                        "owner_id": owner_id,
                        "agent_id": agent_id,
                        "chat_id": chat_id,
                        "title": encrypt_field(title),
                        "model": TitleSource.MODEL.value,
                        "provisional": TitleSource.PROVISIONAL.value,
                    },
                )
            ).rowcount
        return bool(written)

    async def rename_chat(self, owner_id: str, agent_id: str, chat_id: str, *, title: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE chats SET title = :title, title_source = :owner "
                    "WHERE owner_id = :owner_id AND agent_id = :agent_id AND chat_id = :chat_id"
                ),
                {
                    "owner_id": owner_id,
                    "agent_id": agent_id,
                    "chat_id": chat_id,
                    "title": encrypt_field(title),
                    "owner": TitleSource.OWNER.value,
                },
            )

    async def set_chat_active(self, owner_id: str, agent_id: str, chat_id: str, *, is_active: bool) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE chats SET is_active = :is_active "
                    "WHERE owner_id = :owner_id AND agent_id = :agent_id AND chat_id = :chat_id"
                ),
                {"owner_id": owner_id, "agent_id": agent_id, "chat_id": chat_id, "is_active": bool(is_active)},
            )


__all__ = ["SqlChatRegistryRepo", "SqlChatRegistryStore"]
