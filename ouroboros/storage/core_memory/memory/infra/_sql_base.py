from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from new_core.identity import OwnerScope
from ouroboros.storage.core_memory._shared.sql import SqlAdapterBase
from ouroboros.storage.core_memory.memory.domain.models import MemoryHeader
from ouroboros.storage.core_memory.memory.interfaces.memory import IMemory, MemoryRow
from ouroboros.storage.dialect import execution_authority_sql

_OWNED = "owner_id=:owner_id AND agent_id=:agent_id"

_SELECT_SQL = """
    SELECT owner_id, agent_id, kind, name, description, content, status, source,
           first_recorded_at, updated_at, last_recalled_at, recall_count
    FROM memory
    WHERE owner_id=:owner_id AND agent_id=:agent_id AND name=:name
    """


_LIST_HEADERS_SQL = """
    SELECT kind, name, description, status, source,
           first_recorded_at, updated_at, last_recalled_at, recall_count
    FROM memory
    WHERE owner_id=:owner_id AND agent_id=:agent_id
    ORDER BY updated_at DESC, name ASC
    """

_LIST_HEADERS_BY_STATUS_SQL = text(
    """
    SELECT kind, name, description, status, source,
           first_recorded_at, updated_at, last_recalled_at, recall_count
    FROM memory
    WHERE owner_id=:owner_id AND agent_id=:agent_id AND status IN :statuses
    ORDER BY updated_at DESC, name ASC
    """
).bindparams(bindparam("statuses", expanding=True))

_DELETE_SQL = """
    DELETE FROM memory
    WHERE owner_id=:owner_id AND agent_id=:agent_id AND name=:name
    """

_BUMP_RECALL_SQL = """
    UPDATE memory SET last_recalled_at=:now, recall_count=recall_count+1
    WHERE owner_id=:owner_id AND agent_id=:agent_id AND name=:name
    """

_SET_STATUS_SQL = """
    UPDATE memory SET status=:status
    WHERE owner_id=:owner_id AND agent_id=:agent_id AND name=:name
    """


class SqlMemory(SqlAdapterBase, IMemory):
    """``IMemory`` over the runtime DB engine."""

    upsert_sql: str

    begin_sql: str

    lock_select_sql: str

    kind_lock_sql: str

    @staticmethod
    def _owned(scope: OwnerScope, **extra: Any) -> dict[str, Any]:
        """Bind parameters for one scope's rows. Every statement here goes through it."""
        return {"owner_id": scope.owner_id, "agent_id": scope.agent_id, **extra}

    def upsert(self, scope: OwnerScope, row: MemoryRow) -> None:
        with self._engine.connect() as conn:
            conn.execute(text(self.upsert_sql), self._params(scope, row))
            conn.commit()

    def fetch(self, scope: OwnerScope, name: str) -> MemoryRow | None:
        with self._engine.connect() as conn:
            record = conn.execute(text(_SELECT_SQL), self._owned(scope, name=name)).mappings().fetchone()
        return self._to_row(record) if record is not None else None

    def delete(self, scope: OwnerScope, name: str, *, task_id: str = "", claim_token: str = "") -> bool:
        with self._transaction(scope, name, task_id=task_id, claim_token=claim_token) as conn:
            result = conn.execute(text(_DELETE_SQL), self._owned(scope, name=name))
        return int(result.rowcount or 0) > 0

    @contextmanager
    def _transaction(
        self, scope: OwnerScope, name: str, *, kind: str = "", task_id: str = "", claim_token: str = ""
    ) -> Iterator[Connection]:
        params = self._owned(scope, name=name, kind=kind, task_id=task_id, claim_token=claim_token)
        authority = text(
            f"SELECT 1 FROM tasks WHERE {_OWNED} AND task_id=:task_id AND detached "
            f"AND {execution_authority_sql()} AND stop_requested_at IS NULL"
        )
        with self._engine.connect() as conn:
            conn.execute(text(self.begin_sql))
            if kind:
                conn.execute(text(self.kind_lock_sql), params)
            # An absent name needs serialization without materializing a memory before authority is known.
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:name_key, 0))"),
                {"name_key": json.dumps(["memory", scope.owner_id, scope.agent_id, name])},
            )
            conn.execute(
                text(f"SELECT name FROM memory WHERE {_OWNED} AND (kind=:kind OR name=:name) ORDER BY name FOR UPDATE"),
                params,
            )
            if task_id or claim_token:
                conn.execute(text(f"SELECT task_id FROM tasks WHERE {_OWNED} AND task_id=:task_id FOR UPDATE"), params)
            # Every memory/task lock precedes this read; lifecycle writers share the task row through COMMIT.
            if (task_id or claim_token) and not conn.execute(authority, params).scalar():
                raise ValueError("Task execution authority lost")
            yield conn
            # Lease time can advance during the write/prune callback too; roll back the entire mutation.
            if (task_id or claim_token) and not conn.execute(authority, params).scalar():
                raise ValueError("Task execution authority lost")
            conn.commit()

    def list_headers(self, scope: OwnerScope, *, statuses: frozenset[str] | None = None) -> list[MemoryHeader]:
        with self._engine.connect() as conn:
            return self._headers_on_conn(conn, scope, statuses)

    def _headers_on_conn(self, conn: Any, scope: OwnerScope, statuses: frozenset[str] | None) -> list[MemoryHeader]:
        """The header listing on an already-open connection — body-free by construction.

        Used by :meth:`list_headers` on its own connection and by
        :meth:`write_and_prune` inside the write transaction, so the prune reads
        the row it just wrote and never selects ``content``.
        """
        params: dict[str, Any] = self._owned(scope)
        if statuses is None:
            statement = text(_LIST_HEADERS_SQL)
        else:
            if not statuses:
                return []
            statement = _LIST_HEADERS_BY_STATUS_SQL
            params["statuses"] = sorted(statuses)
        records = conn.execute(statement, params).mappings().fetchall()
        return [self._to_header(record) for record in records]

    def write_and_prune(
        self,
        scope: OwnerScope,
        kind: str,
        name: str,
        revise: Callable[[MemoryRow | None], MemoryRow | None],
        prune: Callable[[list[MemoryHeader]], list[str]],
        *,
        statuses: frozenset[str],
        task_id: str = "",
        claim_token: str = "",
    ) -> None:
        params = self._owned(scope, name=name)
        with self._transaction(scope, name, kind=kind, task_id=task_id, claim_token=claim_token) as conn:
            self._write_locked(conn, scope, params, revise)
            headers = self._headers_on_conn(conn, scope, statuses)
            for victim in prune(headers):
                conn.execute(text(_SET_STATUS_SQL), self._owned(scope, status="archived", name=victim))

    def _write_locked(
        self,
        conn: Any,
        scope: OwnerScope,
        params: dict[str, Any],
        revise: Callable[[MemoryRow | None], MemoryRow | None],
    ) -> None:
        """Read and revise under the transaction's name/cohort locks, without placeholder writes."""
        record = conn.execute(text(self.lock_select_sql), params).mappings().fetchone()
        existing = None if record is None else self._to_row(record)
        revised = revise(existing)
        if revised is not None:
            conn.execute(text(self.upsert_sql), self._params(scope, revised))

    def bump_recall(self, scope: OwnerScope, names: list[str], now: float) -> None:
        if not names:
            return
        with self._engine.connect() as conn:
            for name in names:
                conn.execute(text(_BUMP_RECALL_SQL), self._owned(scope, now=float(now), name=name))
            conn.commit()

    def set_status(self, scope: OwnerScope, names: list[str], status: str) -> None:
        if not names:
            return
        with self._engine.connect() as conn:
            for name in names:
                conn.execute(text(_SET_STATUS_SQL), self._owned(scope, status=status, name=name))
            conn.commit()

    # ------------------------------------------------------------------ mapping

    @staticmethod
    def _params(scope: OwnerScope, row: MemoryRow) -> dict[str, Any]:
        return {
            "owner_id": scope.owner_id,
            "agent_id": scope.agent_id,
            "kind": row.kind,
            "name": row.name,
            "description": row.description,
            "content": row.content,
            "status": row.status,
            "source": row.source,
            "first_recorded_at": float(row.first_recorded_at or 0.0),
            "updated_at": float(row.updated_at or 0.0),
            "last_recalled_at": float(row.last_recalled_at or 0.0),
            "recall_count": int(row.recall_count or 0),
        }

    @staticmethod
    def _to_row(record: Any) -> MemoryRow:
        return MemoryRow(
            name=str(record["name"]),
            kind=str(record["kind"] or ""),
            description=str(record["description"] or ""),
            content=str(record["content"] or ""),
            status=str(record["status"] or "active"),
            source=str(record["source"] or ""),
            first_recorded_at=float(record["first_recorded_at"] or 0.0),
            updated_at=float(record["updated_at"] or 0.0),
            last_recalled_at=float(record["last_recalled_at"] or 0.0),
            recall_count=int(record["recall_count"] or 0),
        )

    @staticmethod
    def _to_header(record: Any) -> MemoryHeader:
        return MemoryHeader(
            name=str(record["name"]),
            kind=str(record["kind"] or ""),
            description=str(record["description"] or ""),
            updated_at=float(record["updated_at"] or 0.0),
            first_recorded_at=float(record["first_recorded_at"] or record["updated_at"] or 0.0),
            source=str(record["source"] or ""),
            status=str(record["status"] or "active"),
            last_recalled_at=float(record["last_recalled_at"] or 0.0),
            recall_count=int(record["recall_count"] or 0),
        )


__all__ = ["SqlMemory"]
