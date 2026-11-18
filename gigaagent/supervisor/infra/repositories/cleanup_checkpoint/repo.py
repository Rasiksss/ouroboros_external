from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.engine import Engine

from infra.sql_connection import sql_connection
from new_core.identity import OwnerScope
from ouroboros.storage.runtime_serialization import dump_json, load_json
from ouroboros.utils import utc_now_iso
from supervisor.interfaces.cleanup_checkpoint import (
    CLEANUP_CHECKPOINT_KEY,
    CleanupCheckpoint,
    ICleanupCheckpointRepository,
)

_WRITE_ATTEMPTS = 5


class SqlCleanupCheckpointRepository(ICleanupCheckpointRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextmanager
    def try_claim(self) -> Iterator[bool]:
        with sql_connection(self._engine) as conn, conn.begin():
            claimed = conn.execute(
                text(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(current_schema() || ':ouroboros:working-retrieval-cleanup', 0))"
                )
            ).scalar_one()
            yield bool(claimed)

    def get_last_run_at(self, scope: OwnerScope) -> str:
        stored, _seen = self._read(scope)
        checkpoint = (stored or {}).get(CLEANUP_CHECKPOINT_KEY)
        return str(checkpoint.get("last_run_at") or "") if isinstance(checkpoint, dict) else ""

    def save_checkpoint(self, checkpoint: CleanupCheckpoint, scope: OwnerScope) -> None:
        for _attempt in range(_WRITE_ATTEMPTS):
            stored, seen_body = self._read(scope)
            updated = dict(stored) if stored is not None else {}
            updated[CLEANUP_CHECKPOINT_KEY] = {
                "last_run_at": checkpoint.last_run_at,
                "removed": dict(checkpoint.removed),
            }
            if self._write(scope, dump_json(updated), utc_now_iso(), seen_body=seen_body):
                return
        raise RuntimeError(
            f"owner runtime state for {scope.owner_id}/{scope.agent_id} is being rewritten faster than it can be merged"
        )

    def _read(self, scope: OwnerScope) -> tuple[dict[str, JsonValue] | None, str | None]:
        with sql_connection(self._engine) as conn:
            row = conn.execute(
                text("SELECT payload_json FROM owner_runtime_state WHERE owner_id=:owner_id AND agent_id=:agent_id"),
                {"owner_id": scope.owner_id, "agent_id": scope.agent_id},
            ).fetchone()
        if row is None:
            return None, None
        body = str(row[0])
        return load_json(body), body

    def _write(self, scope: OwnerScope, body: str, now: str, *, seen_body: str | None) -> bool:
        """Write the row, conditional on it still holding what the caller merged from."""
        with sql_connection(self._engine) as conn:
            if seen_body is None:
                applied = conn.execute(
                    text(
                        """
                        INSERT INTO owner_runtime_state (owner_id, agent_id, payload_json, updated_at)
                        VALUES (:owner_id, :agent_id, :payload_json, :updated_at)
                        ON CONFLICT(owner_id, agent_id) DO NOTHING
                        """
                    ),
                    {"owner_id": scope.owner_id, "agent_id": scope.agent_id, "payload_json": body, "updated_at": now},
                ).rowcount
            else:
                applied = conn.execute(
                    text(
                        """
                        UPDATE owner_runtime_state SET payload_json=:payload_json, updated_at=:updated_at
                        WHERE owner_id=:owner_id AND agent_id=:agent_id AND payload_json=:seen_body
                        """
                    ),
                    {
                        "owner_id": scope.owner_id,
                        "agent_id": scope.agent_id,
                        "payload_json": body,
                        "updated_at": now,
                        "seen_body": seen_body,
                    },
                ).rowcount
            conn.commit()
        return bool(applied)
