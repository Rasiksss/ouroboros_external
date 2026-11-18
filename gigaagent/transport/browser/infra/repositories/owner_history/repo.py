from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncConnection

from infra.sql_connection import sql_connection
from new_core.identity import OwnerScope
from ouroboros.contracts.chat_id_policy import is_internal_chat_id
from ouroboros.contracts.tool_confirmation import CONFIRMATION_PENDING
from ouroboros.storage.chat_store import load_chat_history
from ouroboros.storage.file_catalog import hydrate_attachments
from ouroboros.storage.message_history import HistoryWindow
from ouroboros.storage.runtime_models import ConversationMessage
from ouroboros.storage.runtime_rows import message_from_row
from transport.browser.interfaces.owner_history import IOwnerHistoryRepository

_CHATTED = "owner_id=:owner_id AND agent_id=:agent_id AND chat_id=:chat_id"
_SCOPED = "owner_id=:owner_id AND agent_id=:agent_id AND surface_id=:surface_id AND chat_id=:chat_id"


class SqlOwnerHistoryRepository(IOwnerHistoryRepository):
    def __init__(
        self, engine: Engine, *, history_transactions: Callable[[], AbstractAsyncContextManager[AsyncConnection]]
    ) -> None:
        self._engine = engine
        self._history_transactions = history_transactions

    def list_owner_messages_with_open_confirmation(
        self, *, scope: OwnerScope, limit: int
    ) -> tuple[list[ConversationMessage], str | None]:
        """The newest ``limit`` messages of the owner's view, progress included, in the order they
        were said, and the id of the confirmation request the chat is waiting on."""
        predicate = _CHATTED
        if is_internal_chat_id(scope.chat_id):
            predicate += " AND chat_history_visible"
        with sql_connection(self._engine) as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT messages.*, (
                        SELECT confirmation_id FROM tool_confirmations
                        WHERE {_SCOPED} AND status='{CONFIRMATION_PENDING}'
                    ) AS open_confirmation_id
                    FROM messages WHERE {predicate}
                    ORDER BY created_at DESC, message_id DESC
                    LIMIT :limit
                    """
                ),
                {"limit": int(limit), **scope.to_dict()},
            ).fetchall()
            messages = hydrate_attachments(conn, [message_from_row(row) for row in reversed(rows)])
        open_confirmation_id = rows[0]._mapping["open_confirmation_id"] if rows else None
        return messages, open_confirmation_id

    async def load_history(self, *, scope: OwnerScope, limit: int, before_ts: str = "") -> HistoryWindow:
        async with self._history_transactions() as conn:
            return await conn.run_sync(lambda c: load_chat_history(c, scope, limit=limit, before_ts=before_ts))
