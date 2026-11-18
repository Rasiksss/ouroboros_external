"""Atomic scratchpad partitions over the runtime database."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from new_core.identity import OwnerScope
from ouroboros.storage.core_memory._shared.sql import SqlAdapterBase
from ouroboros.storage.core_memory.scratchpad.interfaces.scratchpad import BlockRow, IScratchpad
from ouroboros.storage.dialect import execution_authority_sql
from ouroboros.storage.runtime_models import ACTIVE_STATUSES

_OWNED = "owner_id=:owner_id AND agent_id=:agent_id AND job_id=:job_id"
_PARTITION = f"{_OWNED} AND surface_id=:surface_id AND chat_id=:chat_id"
_INSERT_SQL = (
    "INSERT INTO scratchpad_blocks (owner_id, agent_id, job_id, surface_id, chat_id, ts, source, content, status) "
    "SELECT :owner_id, :agent_id, :job_id, :surface_id, :chat_id, :ts, :source, :content, 'active'"
)
_LIST_ACTIVE_SQL = (
    f"SELECT id, ts, source, content, status FROM scratchpad_blocks WHERE {_PARTITION} AND status='active' ORDER BY id"
)
_EVICT_OVER_CAP_SQL = (
    f"UPDATE scratchpad_blocks SET status='evicted' WHERE {_PARTITION} AND status='active' "
    f"AND id NOT IN (SELECT id FROM scratchpad_blocks WHERE {_PARTITION} AND status='active' ORDER BY id DESC LIMIT :cap)"
)


class SqlScratchpad(SqlAdapterBase, IScratchpad):
    @staticmethod
    def _owned(scope: OwnerScope, *, job_id: str = "", **extra: Any) -> dict[str, Any]:
        return {
            "owner_id": scope.owner_id,
            "agent_id": scope.agent_id,
            "job_id": job_id,
            "surface_id": "" if job_id else str(scope.surface_id),
            "chat_id": "" if job_id else scope.chat_id,
            **extra,
        }

    @contextmanager
    def _partition(self, scope: OwnerScope, job_id: str) -> Iterator[Connection]:
        with self._engine.begin() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:partition, 0))"),
                {"partition": json.dumps(self._owned(scope, job_id=job_id), sort_keys=True)},
            )
            yield conn

    def _job_active(self, conn: Connection, scope: OwnerScope, job_id: str) -> bool:
        row = conn.execute(
            text(f"SELECT status, detached FROM tasks WHERE {_OWNED} ORDER BY lineage_ordinal DESC LIMIT 1"),
            self._owned(scope, job_id=job_id),
        ).fetchone()
        return bool(row and row.detached and row.status in ACTIVE_STATUSES)

    def _write(
        self,
        scope: OwnerScope,
        blocks: Sequence[BlockRow],
        cap: int,
        job_id: str,
        *,
        replace: bool,
        task_id: str,
        claim_token: str,
    ) -> int:
        with self._partition(scope, job_id) as conn:
            params = self._owned(scope, job_id=job_id, task_id=task_id, claim_token=claim_token, cap=int(cap))
            authority = ""
            if job_id:
                # Share the task row with lifecycle writers; unrelated partitions stay independent.
                conn.execute(
                    text(f"SELECT task_id FROM tasks WHERE task_id=:task_id AND {_OWNED} FOR UPDATE"),
                    params,
                )
                authority = (
                    f"EXISTS (SELECT 1 FROM tasks WHERE {_OWNED} AND task_id=:task_id AND detached AND "
                    f"{execution_authority_sql()} AND stop_requested_at IS NULL)"
                )
            if replace:
                conn.execute(
                    text(f"UPDATE scratchpad_blocks SET status='evicted' WHERE {_PARTITION} AND status='active'"),
                    self._owned(scope, job_id=job_id),
                )
            for block in blocks:
                if not conn.execute(
                    text(_INSERT_SQL + (f" WHERE {authority}" if authority else "")),
                    dict(params, ts=block.ts, source=block.source, content=block.content),
                ).rowcount:
                    raise ValueError("scratchpad job partition is closed")
            evicted = conn.execute(text(_EVICT_OVER_CAP_SQL), params).rowcount
            if authority and not conn.execute(text(f"SELECT 1 WHERE {authority}"), params).scalar():
                raise ValueError("scratchpad job partition is closed")
        return int(evicted or 0)

    def append_and_evict(
        self,
        scope: OwnerScope,
        ts: str,
        source: str,
        content: str,
        cap: int,
        *,
        job_id: str = "",
        task_id: str = "",
        claim_token: str = "",
    ) -> int:
        return self._write(
            scope,
            [BlockRow(0, ts, source, content)],
            cap,
            job_id,
            replace=False,
            task_id=task_id,
            claim_token=claim_token,
        )

    def replace_blocks(
        self,
        scope: OwnerScope,
        blocks: Sequence[BlockRow],
        cap: int,
        *,
        job_id: str = "",
        task_id: str = "",
        claim_token: str = "",
    ) -> None:
        self._write(scope, blocks, cap, job_id, replace=True, task_id=task_id, claim_token=claim_token)

    def list_active(self, scope: OwnerScope, *, job_id: str = "", conn: Connection | None = None) -> list[BlockRow]:
        with nullcontext(conn) if conn is not None else self._engine.connect() as conn:
            records = conn.execute(text(_LIST_ACTIVE_SQL), self._owned(scope, job_id=job_id)).mappings().fetchall()
        return [BlockRow(**record) for record in records]

    def delete_partition(self, scope: OwnerScope, *, job_id: str = "", limit: int) -> int:
        if not job_id:
            raise ValueError("the foreground scratchpad cannot be deleted as a job partition")
        with self._partition(scope, job_id) as conn:
            if self._job_active(conn, scope, job_id):
                return 0
            deleted = conn.execute(
                text(
                    f"DELETE FROM scratchpad_blocks WHERE {_PARTITION} AND id IN "
                    f"(SELECT id FROM scratchpad_blocks WHERE {_PARTITION} ORDER BY id LIMIT :limit)"
                ),
                self._owned(scope, job_id=job_id, limit=max(0, int(limit))),
            ).rowcount
        return int(deleted or 0)
