from __future__ import annotations

from sqlalchemy import RowMapping, bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine

from new_core.identity import OwnerScope
from ouroboros.storage.core_memory.memory.domain.models import PROMPT_MEMORY_STATUSES, MemoryHeader


def _to_header(row: RowMapping) -> MemoryHeader:
    return MemoryHeader(
        name=str(row["name"]),
        kind=str(row["kind"] or ""),
        description=str(row["description"] or ""),
        updated_at=float(row["updated_at"] or 0.0),
        first_recorded_at=float(row["first_recorded_at"] or row["updated_at"] or 0.0),
        source=str(row["source"] or ""),
        status=str(row["status"] or "active"),
        last_recalled_at=float(row["last_recalled_at"] or 0.0),
        recall_count=int(row["recall_count"] or 0),
    )


class SqlMemoryHeaderRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_prompt_headers(self, scope: OwnerScope) -> list[MemoryHeader]:
        if not PROMPT_MEMORY_STATUSES:
            return []
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT kind, name, description, status, source,
                           first_recorded_at, updated_at, last_recalled_at, recall_count
                    FROM memory
                    WHERE owner_id=:owner_id AND agent_id=:agent_id AND status IN :statuses
                    ORDER BY updated_at DESC, name ASC
                    """
                ).bindparams(bindparam("statuses", expanding=True)),
                {
                    "owner_id": scope.owner_id,
                    "agent_id": scope.agent_id,
                    "statuses": sorted(PROMPT_MEMORY_STATUSES),
                },
            )
            records = result.mappings().all()
        return [_to_header(record) for record in records]


__all__ = ["SqlMemoryHeaderRepo"]
