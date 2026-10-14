from sqlalchemy import text
from sqlalchemy.engine import Engine

from infra.sql_connection import sql_connection
from new_core.identity import ServiceJob, ServiceScope
from ouroboros.storage.core_memory.scratchpad.interfaces.scratchpad import IScratchpad
from ouroboros.storage.dialect import ACTIVE_STATUS_SQL
from ouroboros.utils import utc_now_iso
from supervisor.interfaces.working_retrieval import IWorkingRetrievalRepository


class SqlWorkingRetrievalRepository(IWorkingRetrievalRepository):
    def __init__(self, engine: Engine, *, scratchpad: IScratchpad) -> None:
        self._engine = engine
        self._scratchpad = scratchpad

    def cleanup_expired_working_retrieval(
        self,
        *,
        now_iso: str,
        limit: int = 5000,
        cleanup_retrieval: bool = True,
    ) -> dict[str, int]:
        now = str(now_iso or utc_now_iso())
        capped = max(1, int(limit or 1))
        # Select-then-delete by id, and idempotent with it: a row another sweeper removed in
        # between is simply not deleted twice. Nothing here decides from what it read.
        removed: dict[str, int] = {}
        with sql_connection(self._engine) as conn:
            tables = (
                (
                    ("task_episodes", "episode_id"),
                    ("tool_invocations", "tool_invocation_id"),
                )
                if cleanup_retrieval
                else ()
            )
            for table, id_column in tables:
                rows = conn.execute(
                    text(f"SELECT {id_column} FROM {table} WHERE expires_at != '' AND expires_at <= :now LIMIT :limit"),
                    {"now": now, "limit": capped},
                ).fetchall()
                ids = [str(row[0]) for row in rows]
                if not ids:
                    removed[table] = 0
                    continue
                id_params = {f"id_{index}": value for index, value in enumerate(ids)}
                placeholders = ", ".join(f":id_{index}" for index in range(len(ids)))
                result = conn.execute(
                    text(f"DELETE FROM {table} WHERE {id_column} IN ({placeholders})"),
                    id_params,
                )
                removed[table] = int(result.rowcount or 0)
            partitions = conn.execute(
                text(
                    "SELECT DISTINCT s.owner_id, s.agent_id, s.job_id FROM scratchpad_blocks s WHERE s.job_id<>'' "
                    "AND COALESCE((SELECT t.status FROM tasks t WHERE t.owner_id=s.owner_id AND t.agent_id=s.agent_id "
                    "AND t.job_id=s.job_id ORDER BY t.lineage_ordinal DESC LIMIT 1), '') "
                    f"NOT IN ({ACTIVE_STATUS_SQL}) LIMIT :limit"
                ),
                {"limit": min(50, capped)},
            ).fetchall()
            conn.commit()
        # The adapter shares cleanup's lock with append/replace, including writers already in flight.
        scratchpad = self._scratchpad
        removed["scratchpad_blocks"] = 0
        for partition in partitions:
            scope = ServiceScope(
                owner_id=partition.owner_id, agent_id=partition.agent_id, job=ServiceJob.CONTEXT_CLEANUP
            ).as_scope()
            removed["scratchpad_blocks"] += scratchpad.delete_partition(
                scope, job_id=partition.job_id, limit=capped - removed["scratchpad_blocks"]
            )
        return removed
