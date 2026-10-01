"""``IScratchpad`` over the runtime DB engine.

Base plus a thin ``SqliteScratchpad``/``PsqlScratchpad`` pair. Almost every
statement here is dialect-neutral — the database allocates the block order key
(design D8), so nothing needs a dialect-specific ``INSERT ... ON CONFLICT`` or
``RETURNING`` clause the way long-term memory's upsert does. The one dialect
difference is ``owner_lock_sql``: the per-``owner_id`` serialization clause run
first in ``append_and_evict``, which PostgreSQL needs and SQLite does not.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from ouroboros.storage.core_memory._shared.sql import SqlAdapterBase
from ouroboros.storage.core_memory.scratchpad.interfaces.scratchpad import BlockRow, IScratchpad

_TABLE = "scratchpad_blocks"

# The order key is omitted on purpose: the database allocates it, so two
# concurrent appends cannot compute the same value and collide (design D8).
_INSERT_SQL = (
    f"INSERT INTO {_TABLE} (owner_id, ts, source, content, status) VALUES (:owner_id, :ts, :source, :content, 'active')"
)

_LIST_ACTIVE_SQL = (
    f"SELECT id, owner_id, ts, source, content, status FROM {_TABLE} "
    "WHERE owner_id=:owner_id AND status='active' ORDER BY id"
)

# Bounded update instead of a delete: evicted bodies stay queryable alongside
# live blocks, which is what replaced the eviction journal (design D9).
_EVICT_OVER_CAP_SQL = (
    f"UPDATE {_TABLE} SET status='evicted' WHERE owner_id=:owner_id AND status='active' "
    f"AND id NOT IN (SELECT id FROM {_TABLE} WHERE owner_id=:owner_id AND status='active' "
    "ORDER BY id DESC LIMIT :cap)"
)


class SqlScratchpad(SqlAdapterBase, IScratchpad):
    """Scratchpad blocks in the runtime DB — base for both dialect adapters."""

    #: Optional statement, run first in :meth:`append_and_evict`, that serializes
    #: the append-and-evict transaction by ``owner_id``. Empty where the
    #: transaction's first write (the insert) already excludes concurrent
    #: same-owner appenders — SQLite's whole-database write lock. Bound with
    #: ``owner_id``.
    owner_lock_sql: str = ""

    def append_and_evict(self, owner_id: str, ts: str, source: str, content: str, cap: int) -> int:
        # ``begin()`` rather than ``connect()`` plus two commits: the insert and
        # the eviction are one unit of work, so they commit together or not at
        # all.
        with self._engine.begin() as conn:
            # Serialize same-owner appends before the insert: a second appender
            # waits here and then evicts against a snapshot that already includes
            # the first insert, so the cap holds under concurrency rather than
            # being a race outcome. Empty on SQLite, whose insert (the first
            # write) already takes the whole-database write lock.
            if self.owner_lock_sql:
                conn.execute(text(self.owner_lock_sql), {"owner_id": owner_id})
            conn.execute(
                text(_INSERT_SQL),
                {"owner_id": owner_id, "ts": ts, "source": source, "content": content},
            )
            # Read inside the transaction: the result is closed with it.
            evicted = conn.execute(text(_EVICT_OVER_CAP_SQL), {"owner_id": owner_id, "cap": int(cap)}).rowcount
        return int(evicted or 0)

    def list_active(self, owner_id: str) -> list[BlockRow]:
        with self._engine.connect() as conn:
            records = conn.execute(text(_LIST_ACTIVE_SQL), {"owner_id": owner_id}).mappings().fetchall()
        return [self._to_row(record) for record in records]

    # ----------------------------------------------------------------- mapping

    @staticmethod
    def _to_row(record: Any) -> BlockRow:
        return BlockRow(
            id=int(record["id"]),
            owner_id=str(record["owner_id"]),
            ts=str(record["ts"] or ""),
            source=str(record["source"] or ""),
            content=str(record["content"] or ""),
            status=str(record["status"] or "active"),
        )


class SqliteScratchpad(SqlScratchpad):
    """Scratchpad blocks in the SQLite runtime DB.

    ``owner_lock_sql`` stays empty: the append's first statement is the insert —
    a write — so it takes the whole-database write lock at statement one, a
    strict superset of per-``owner_id`` serialization. A second appender blocks
    at its own insert (30s ``busy_timeout``) until the first commits, then evicts
    against a snapshot that already includes it. No ``BEGIN IMMEDIATE`` is needed
    precisely because the write comes first (unlike memory's ``revise``, which
    reads before writing and therefore does need it).
    """


class PsqlScratchpad(SqlScratchpad):
    """Scratchpad blocks in the PostgreSQL runtime DB."""

    # A transaction-scoped advisory lock keyed on ``owner_id``, taken before the
    # insert. It serializes same-owner appends: the second appender waits here
    # and then decides its eviction against a snapshot that already includes the
    # first insert. Without it, under READ COMMITTED (the default) two concurrent
    # appends do not see each other's uncommitted insert, so each counts the
    # buffer as one over the cap and each evicts the same single oldest block —
    # leaving the buffer over its cap once both commit; a plain single
    # transaction gives atomicity but not this isolation. The lock auto-releases
    # on commit or rollback, so a failed append leaks no lock, and distinct
    # ``owner_id`` keys let different owners proceed in parallel. The single-arg
    # form lives in a different lock space from memory's two-arg
    # ``pg_advisory_xact_lock(hashtext(owner), hashtext(kind))``, so scratchpad
    # and memory locks never collide even for the same owner (``hashtext``
    # returns int4 and widens to the bigint the single-arg form takes).
    owner_lock_sql = "SELECT pg_advisory_xact_lock(hashtext(:owner_id))"


__all__ = ["PsqlScratchpad", "SqlScratchpad", "SqliteScratchpad"]
