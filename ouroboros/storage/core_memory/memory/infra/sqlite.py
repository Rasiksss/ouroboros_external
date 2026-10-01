"""SQLite implementation of the unified memory I/O port."""

from __future__ import annotations

from ouroboros.storage.core_memory.memory.infra._sql_base import SqlMemory

_UPSERT_SQL = """
    INSERT INTO memory (
        owner_id, kind, name, description, content, status, source,
        first_recorded_at, updated_at, last_recalled_at, recall_count
    ) VALUES (
        :owner_id, :kind, :name, :description, :content, :status, :source,
        :first_recorded_at, :updated_at, :last_recalled_at, :recall_count
    )
    ON CONFLICT(owner_id, name) DO UPDATE SET
        kind=excluded.kind,
        description=excluded.description,
        content=excluded.content,
        status=excluded.status,
        source=excluded.source,
        first_recorded_at=excluded.first_recorded_at,
        updated_at=excluded.updated_at,
        last_recalled_at=excluded.last_recalled_at,
        recall_count=excluded.recall_count
    """


class SqliteMemory(SqlMemory):
    """Memory rows in the SQLite runtime DB.

    ``BEGIN IMMEDIATE`` takes the database write lock at statement one, so a
    second writer blocks there rather than reading content it is about to
    overwrite. The locking select needs no clause of its own — the write lock is
    already held — and no row has to be materialized to give it something to
    lock, so ``revise`` sees ``None`` for an absent name.

    ``kind_lock_sql`` stays empty for the same reason: the whole-database write
    lock ``BEGIN IMMEDIATE`` already holds serializes every writer, which is a
    strict superset of ``write_and_prune``'s ``(owner_id, kind)`` serialization.
    """

    upsert_sql = _UPSERT_SQL
    begin_sql = "BEGIN IMMEDIATE"


__all__ = ["SqliteMemory"]
