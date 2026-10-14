from __future__ import annotations

import base64

from pydantic import BaseModel
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from new_core.identity import OwnerScope
from ouroboros.storage.file_catalog import FileCatalog, FileError, FileFailure
from ouroboros.storage.message_history import HistoryWindow
from ouroboros.storage.runtime_models import ConversationMessage, source_aliases


class MessageLink(BaseModel):
    chat_id: str
    message_id: str
    created_at: str
    excerpt: str


class MessagePage(BaseModel):
    items: list[MessageLink]
    next_cursor: str | None = None

    @classmethod
    def from_messages(cls, messages: list[ConversationMessage], *, file_id: str, more: bool) -> MessagePage:
        items = [
            MessageLink(
                chat_id=m.chat_id, message_id=m.message_id, created_at=m.created_at, excerpt=m.display_text()[:200]
            )
            for m in messages
        ]
        cursor = None
        if more:
            last = items[-1]
            cursor = base64.urlsafe_b64encode("\0".join((file_id, last.created_at, last.message_id)).encode()).decode()
        return cls(items=items, next_cursor=cursor)


class FileHistory:
    def __init__(self, connection: Connection, scope: OwnerScope) -> None:
        self.conn, self.scope = connection, scope

    def file_messages(self, file_id: str, *, cursor: str = "", limit: int = 50) -> HistoryWindow:
        conn, scope = self.conn, self.scope
        FileCatalog(conn, scope).file(file_id, available=False)
        if not 1 <= limit <= 100:
            raise FileError("invalid limit", FileFailure.INVALID_INPUT)
        stamp, message_id = "", ""
        if cursor:
            try:
                bound, stamp, message_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("\0")
                if bound != file_id:
                    raise ValueError("different file")
            except (ValueError, UnicodeError) as exc:
                raise FileError("invalid cursor", FileFailure.INVALID_INPUT) from exc
        rows = (
            conn.execute(
                text(
                    "SELECT m.* FROM messages m JOIN message_files f ON f.message_id=m.message_id "
                    "WHERE f.file_id=:file AND m.owner_id=:owner AND m.agent_id=:agent AND m.source IN :sources "
                    "AND m.chat_history_visible=true "
                    "AND (:surface='browser' OR m.chat_id=:chat) AND (m.created_at,m.message_id)>(:stamp,:message) "
                    "ORDER BY m.created_at,m.message_id LIMIT :limit"
                ).bindparams(bindparam("sources", expanding=True)),
                {
                    "file": file_id,
                    "owner": scope.owner_id,
                    "agent": scope.agent_id,
                    "surface": str(scope.surface_id),
                    "sources": source_aliases(scope.surface_id),
                    "chat": scope.chat_id,
                    "stamp": stamp,
                    "message": message_id,
                    "limit": limit + 1,
                },
            )
            .mappings()
            .all()
        )
        return HistoryWindow(list(rows[:limit]), conn.dialect.name, len(rows) > limit)
