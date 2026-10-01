"""Shared SQL plumbing for the dialect-specific memory adapters.

Two things differ between SQLite and PostgreSQL and both live in the subclasses:
the upsert's ``excluded``/``EXCLUDED`` casing, and the way ``revise`` opens its
transaction and locks the row it is about to rewrite. Everything else — fetch,
the header listing, delete, recall, status, and the revise body once the lock is
held — is identical and lives here.

``_SELECT_SQL`` is the only statement in the package that reads ``content``, and
it is keyed by the name the caller named. ``revise``'s locking read is
``_SELECT_SQL`` plus a dialect lock clause, so there is one body-bearing read
shape to audit and no listing among them.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from typing import Any

from sqlalchemy import bindparam, text

from ouroboros.storage.core_memory._shared.sql import SqlAdapterBase
from ouroboros.storage.core_memory.memory.domain.models import MemoryHeader
from ouroboros.storage.core_memory.memory.interfaces.memory import IMemory, MemoryRow

_TABLE = "memory"

_COLUMNS = (
    "owner_id, kind, name, description, content, status, source, "
    "first_recorded_at, updated_at, last_recalled_at, recall_count"
)

#: The single body-bearing read. ``revise`` reuses it verbatim with a lock
#: clause appended; nothing else in the package selects ``content``.
_SELECT_SQL = f"SELECT {_COLUMNS} FROM {_TABLE} WHERE owner_id=:owner_id AND name=:name"

#: Headers only. Bodies run to 80 000 characters and the index needs none of
#: them, so the cheap path is the only path.
_HEADER_COLUMNS = (
    "owner_id, kind, name, description, status, source, first_recorded_at, updated_at, last_recalled_at, recall_count"
)

#: Newest first, name breaking ties — the order the port promises. The status
#: filter is a predicate rather than a Python pass so archived rows, which are
#: never deleted and therefore accumulate forever, stay out of the hot path;
#: ``idx_memory_owner_status_updated`` is declared in exactly this shape, so the
#: prompt listing is an index range scan with no sort.
_LIST_HEADERS_SQL = (
    f"SELECT {_HEADER_COLUMNS} FROM {_TABLE} WHERE owner_id=:owner_id ORDER BY updated_at DESC, name ASC"
)

_LIST_HEADERS_BY_STATUS_SQL = text(
    f"SELECT {_HEADER_COLUMNS} FROM {_TABLE} WHERE owner_id=:owner_id AND status IN :statuses "
    "ORDER BY updated_at DESC, name ASC"
).bindparams(bindparam("statuses", expanding=True))

_DELETE_SQL = f"DELETE FROM {_TABLE} WHERE owner_id=:owner_id AND name=:name"

_BUMP_RECALL_SQL = (
    f"UPDATE {_TABLE} SET last_recalled_at=:now, recall_count=recall_count+1 WHERE owner_id=:owner_id AND name=:name"
)

_SET_STATUS_SQL = f"UPDATE {_TABLE} SET status=:status WHERE owner_id=:owner_id AND name=:name"


class SqlMemory(SqlAdapterBase, IMemory):
    """``IMemory`` over the runtime DB engine."""

    #: Dialect-specific INSERT ... ON CONFLICT DO UPDATE, set by subclasses.
    upsert_sql: str = ""

    #: Statement that opens the revise transaction (``BEGIN`` variants).
    begin_sql: str = "BEGIN"

    #: The fetch statement plus whatever lock clause the dialect needs. On
    #: SQLite the write lock is already taken by ``begin_sql``.
    lock_select_sql: str = _SELECT_SQL

    #: Optional statement giving ``lock_select_sql`` a row to lock. Empty where
    #: the transaction opener already excludes concurrent writers.
    ensure_row_sql: str = ""

    #: Optional statement, run right after ``begin_sql``, that serializes the
    #: transaction by ``(owner_id, kind)`` for :meth:`write_and_prune`. Empty
    #: where the transaction opener already serializes at a coarser granularity
    #: (SQLite's whole-database write lock). Bound with ``owner_id`` and ``kind``.
    kind_lock_sql: str = ""

    def upsert(self, row: MemoryRow) -> None:
        with self._engine.connect() as conn:
            conn.execute(text(self.upsert_sql), self._params(row))
            conn.commit()

    def fetch(self, owner_id: str, name: str) -> MemoryRow | None:
        with self._engine.connect() as conn:
            record = conn.execute(text(_SELECT_SQL), {"owner_id": owner_id, "name": name}).mappings().fetchone()
        return self._to_row(record) if record is not None else None

    def delete(self, owner_id: str, name: str) -> bool:
        with self._engine.connect() as conn:
            result = conn.execute(text(_DELETE_SQL), {"owner_id": owner_id, "name": name})
            conn.commit()
        return int(result.rowcount or 0) > 0

    def list_headers(self, owner_id: str, *, statuses: frozenset[str] | None = None) -> list[MemoryHeader]:
        with self._engine.connect() as conn:
            return self._headers_on_conn(conn, owner_id, statuses)

    def _headers_on_conn(self, conn: Any, owner_id: str, statuses: frozenset[str] | None) -> list[MemoryHeader]:
        """The header listing on an already-open connection — body-free by construction.

        Used by :meth:`list_headers` on its own connection and by
        :meth:`write_and_prune` inside the write transaction, so the prune reads
        the row it just wrote and never selects ``content``.
        """
        params: dict[str, Any] = {"owner_id": owner_id}
        if statuses is None:
            statement = text(_LIST_HEADERS_SQL)
        else:
            if not statuses:
                return []
            statement = _LIST_HEADERS_BY_STATUS_SQL
            params["statuses"] = sorted(statuses)
        records = conn.execute(statement, params).mappings().fetchall()
        return [self._to_header(record) for record in records]

    def revise(
        self,
        owner_id: str,
        name: str,
        revise: Callable[[MemoryRow | None], MemoryRow | None],
        template: MemoryRow,
    ) -> None:
        params = {"owner_id": owner_id, "name": name}
        with self._engine.connect() as conn:
            # One transaction around read, revise, and write. A second writer
            # blocks at the opener (SQLite) or the locking select (PostgreSQL)
            # instead of reading content that is about to be replaced.
            conn.execute(text(self.begin_sql))
            try:
                self._write_locked(conn, params, revise, template)
            except Exception:
                # Suppressed so the original failure is what the caller sees; a
                # connection left mid-transaction is reset on return to the pool.
                with suppress(Exception):
                    conn.execute(text("ROLLBACK"))
                raise
            conn.execute(text("COMMIT"))

    def write_and_prune(
        self,
        owner_id: str,
        kind: str,
        name: str,
        revise: Callable[[MemoryRow | None], MemoryRow | None],
        template: MemoryRow,
        prune: Callable[[list[MemoryHeader]], list[str]],
        *,
        statuses: frozenset[str],
    ) -> None:
        params = {"owner_id": owner_id, "name": name}
        lock_params = {"owner_id": owner_id, "kind": kind}
        with self._engine.connect() as conn:
            # One transaction around the write and its capacity prune, serialized
            # by (owner_id, kind): the kind lock (PostgreSQL) or the whole-DB
            # write lock the opener already holds (SQLite) keeps two same-kind
            # saves from interleaving, so the cohort read below sees a settled
            # count and the eviction it drives cannot be undone by a racing save.
            conn.execute(text(self.begin_sql))
            try:
                if self.kind_lock_sql:
                    conn.execute(text(self.kind_lock_sql), lock_params)
                self._write_locked(conn, params, revise, template)
                # Body-free, same connection: the listing sees the staged write
                # and the caller's ``prune`` picks the victims from it. Archiving
                # rides the same transaction, so the write and the eviction commit
                # together or roll back together.
                headers = self._headers_on_conn(conn, owner_id, statuses)
                for victim in prune(headers):
                    conn.execute(text(_SET_STATUS_SQL), {"status": "archived", "owner_id": owner_id, "name": victim})
            except Exception:
                with suppress(Exception):
                    conn.execute(text("ROLLBACK"))
                raise
            conn.execute(text("COMMIT"))

    def _write_locked(
        self,
        conn: Any,
        params: dict[str, Any],
        revise: Callable[[MemoryRow | None], MemoryRow | None],
        template: MemoryRow,
    ) -> None:
        """Ensure-row, lock the named row, apply ``revise``, and upsert or delete.

        The transaction is already open (and, for :meth:`write_and_prune`, the
        kind lock already held). ``params`` carries ``owner_id`` and ``name``.

        An adapter that materializes a row to lock has to hide it again: the row
        exists for the lock, not for the caller. The statement reports whether it
        inserted, so the callback is handed ``None`` exactly when the name was
        absent — the same thing SQLite hands it — and a callback that declines to
        write leaves nothing behind on either dialect.
        """
        materialized = False
        if self.ensure_row_sql:
            materialized = conn.execute(text(self.ensure_row_sql), self._params(template)).fetchone() is not None
        record = conn.execute(text(self.lock_select_sql), params).mappings().fetchone()
        existing = None if materialized or record is None else self._to_row(record)
        revised = revise(existing)
        if revised is not None:
            conn.execute(text(self.upsert_sql), self._params(revised))
        elif materialized:
            conn.execute(text(_DELETE_SQL), params)

    def bump_recall(self, owner_id: str, names: list[str], now: float) -> None:
        if not names:
            return
        with self._engine.connect() as conn:
            for name in names:
                conn.execute(text(_BUMP_RECALL_SQL), {"now": float(now), "owner_id": owner_id, "name": name})
            conn.commit()

    def set_status(self, owner_id: str, names: list[str], status: str) -> None:
        if not names:
            return
        with self._engine.connect() as conn:
            for name in names:
                conn.execute(text(_SET_STATUS_SQL), {"status": status, "owner_id": owner_id, "name": name})
            conn.commit()

    # ------------------------------------------------------------------ mapping

    @staticmethod
    def _params(row: MemoryRow) -> dict[str, Any]:
        return {
            "owner_id": row.owner_id,
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
            owner_id=str(record["owner_id"]),
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
