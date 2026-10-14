from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import RowMapping, bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine

from background_memory.infra.repositories._rows import load_json
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.storage.message_field_crypto import decrypt_field


@dataclass(frozen=True)
class CurrentJob:
    task_id: str
    status: str
    scope: OwnerScope | None


class SqlTaskRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def is_detached(self, task_id: str) -> bool | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text("SELECT detached FROM tasks WHERE task_id=:task_id"),
                {"task_id": str(task_id)},
            )
            row = result.first()
        return None if row is None else bool(row[0])

    async def current_job(self, job_id: str, *, scope: OwnerScope) -> CurrentJob | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT task_id, status, owner_id, agent_id, source, chat_id, input_json, payload_json
                    FROM tasks
                    WHERE owner_id=:owner_id AND agent_id=:agent_id
                      AND COALESCE(NULLIF(job_id, ''), task_id)=:job_id
                      AND task_type='task' AND parent_task_id=''
                    ORDER BY lineage_ordinal DESC
                    LIMIT 1
                    """
                ),
                {"owner_id": scope.owner_id, "agent_id": scope.agent_id, "job_id": str(job_id)},
            )
            row = result.mappings().first()
        if row is None:
            return None
        stored = self._job_scope(row)
        return CurrentJob(
            task_id=str(row["task_id"] or ""),
            status=str(row["status"] or ""),
            scope=OwnerScope.from_dict(stored) if stored is not None else None,
        )

    async def list_tasks(self, *, statuses: Sequence[str], limit: int) -> list[str]:
        if not statuses:
            return []
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text("SELECT task_id FROM tasks WHERE status IN :statuses LIMIT :limit").bindparams(
                    bindparam("statuses", expanding=True)
                ),
                {"statuses": list(statuses), "limit": max(1, int(limit))},
            )
            rows = result.all()
        return [str(row[0]) for row in rows]

    def _job_scope(self, row: RowMapping) -> dict[str, str] | None:
        payload = load_json(decrypt_field(row["payload_json"]))
        stored_input = payload["input"] if "input" in payload else load_json(decrypt_field(row["input_json"]))
        if isinstance(stored_input, dict) and stored_input.get("scope"):
            return stored_input["scope"]
        chat = str(row["chat_id"] if row["chat_id"] is not None else "").strip()
        surface = SurfaceId.coerce(row["source"])
        if not chat or surface is None:
            return None
        return {
            "owner_id": str(row["owner_id"]),
            "agent_id": str(row["agent_id"]),
            "surface_id": str(surface),
            "chat_id": chat,
        }


__all__ = ["CurrentJob", "SqlTaskRepo"]
