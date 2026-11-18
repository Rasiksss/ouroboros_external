"""PostgreSQL implementation of the unified memory I/O port."""

from __future__ import annotations

from ouroboros.storage.core_memory.memory.infra._sql_base import _SELECT_SQL, SqlMemory

_UPSERT_SQL = """
    INSERT INTO memory (
        owner_id, agent_id, kind, name, description, content, status, source,
        first_recorded_at, updated_at, last_recalled_at, recall_count
    ) VALUES (
        :owner_id, :agent_id, :kind, :name, :description, :content, :status, :source,
        :first_recorded_at, :updated_at, :last_recalled_at, :recall_count
    )
    ON CONFLICT (owner_id, agent_id, name) DO UPDATE SET
        kind=EXCLUDED.kind,
        description=EXCLUDED.description,
        content=EXCLUDED.content,
        status=EXCLUDED.status,
        source=EXCLUDED.source,
        first_recorded_at=EXCLUDED.first_recorded_at,
        updated_at=EXCLUDED.updated_at,
        last_recalled_at=EXCLUDED.last_recalled_at,
        recall_count=EXCLUDED.recall_count
    """

# The one body-bearing read, plus a lock clause. No SKIP LOCKED: a concurrent
# appender must wait for the row and then append to what the first one wrote —
# skipping would drop its fragment. Under READ COMMITTED without FOR UPDATE both
# transactions would read the pre-append content and the later commit would win.
_LOCK_SELECT_SQL = f"{_SELECT_SQL} FOR UPDATE"

# The (owner_id, agent_id, kind) serialization for ``write_and_prune``. The per-name
# ``FOR UPDATE`` above locks one row, which serializes appends to that name but
# NOT two saves of different names in the same kind — their capacity prunes would
# still race and could each archive against a stale cohort. A transaction-scoped
# advisory lock keyed on the pair closes that: same-kind writers serialize, and
# it auto-releases on commit or rollback, so a failed save leaks no lock. The
# two-int form keeps ownership and kind in separate 32-bit halves, so a collision
# needs both halves to collide — and a collision only ever costs an extra wait,
# never correctness. Different kinds (and different owner/agent pairs) hash to
# different keys and run in parallel. Both ownership axes fold into the first half:
# one agent's prune must not serialize against another's.
_KIND_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext(:owner_id || ':' || :agent_id), hashtext(:kind))"


class PsqlMemory(SqlMemory):
    """Memory rows in the PostgreSQL runtime DB."""

    upsert_sql = _UPSERT_SQL
    begin_sql = "BEGIN"
    lock_select_sql = _LOCK_SELECT_SQL
    kind_lock_sql = _KIND_LOCK_SQL


__all__ = ["PsqlMemory"]
