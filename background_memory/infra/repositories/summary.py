from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from background_memory.infra.repositories._rows import dump_json, to_context_summary
from new_core.identity import OwnerScope
from ouroboros.storage.message_field_crypto import encrypt_field
from ouroboros.storage.runtime_models import ContextSummary
from ouroboros.utils import utc_now_iso


class SqlContextSummaryRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def read(self, scope: OwnerScope) -> ContextSummary | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT * FROM context_summaries
                    WHERE owner_id=:owner_id AND agent_id=:agent_id AND chat_id=:chat_id
                    ORDER BY created_at DESC, summary_id DESC
                    LIMIT 1
                    """
                ),
                {"owner_id": scope.owner_id, "agent_id": scope.agent_id, "chat_id": scope.chat_id},
            )
            row = result.mappings().first()
        return to_context_summary(row) if row is not None else None

    async def commit(self, summary: ContextSummary, *, expected_cursor: str) -> bool:
        now = utc_now_iso()
        summary.created_at = now
        params: dict[str, Any] = {
            "summary_id": summary.summary_id,
            "owner_id": summary.owner_id,
            "agent_id": summary.agent_id,
            "chat_id": summary.chat_id,
            "source_offsets_ref": summary.source_offsets_ref,
            "summary_ref": encrypt_field(summary.summary_ref),
            "token_budget": int(summary.token_budget),
            "summary_hash": summary.summary_hash,
            "created_at": now,
            "metadata_json": encrypt_field(dump_json(summary.metadata)),
            "payload_json": encrypt_field(dump_json(summary.model_dump(mode="json"))),
            "expected_cursor": str(expected_cursor or ""),
        }
        async with self._engine.begin() as conn:
            swapped = await conn.execute(
                text(
                    """
                    UPDATE context_summaries SET
                        source_offsets_ref=:source_offsets_ref,
                        summary_ref=:summary_ref, token_budget=:token_budget, summary_hash=:summary_hash,
                        created_at=:created_at, metadata_json=:metadata_json, payload_json=:payload_json
                    WHERE owner_id=:owner_id AND agent_id=:agent_id AND chat_id=:chat_id
                      AND COALESCE(source_offsets_ref, '')=:expected_cursor
                    """
                ),
                params,
            )
            applied = swapped.rowcount
            if not applied and not expected_cursor:
                inserted = await conn.execute(
                    text(
                        """
                        INSERT INTO context_summaries (summary_id, owner_id, agent_id, chat_id, source_offsets_ref,
                            summary_ref, token_budget, summary_hash, created_at, metadata_json, payload_json)
                        VALUES (:summary_id, :owner_id, :agent_id, :chat_id, :source_offsets_ref, :summary_ref,
                            :token_budget, :summary_hash, :created_at, :metadata_json, :payload_json)
                        ON CONFLICT(owner_id, agent_id, chat_id) DO NOTHING
                        """
                    ),
                    params,
                )
                applied = inserted.rowcount
        return int(applied or 0) == 1


__all__ = ["SqlContextSummaryRepo"]
