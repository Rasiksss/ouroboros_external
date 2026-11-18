from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.engine import Engine

from infra.sql_connection import sql_connection
from new_core.identity import OwnerScope
from ouroboros.storage.runtime_serialization import dump_json, load_json
from ouroboros.utils import utc_now_iso
from supervisor.interfaces.cleanup_checkpoint import CLEANUP_CHECKPOINT_KEY
from supervisor.interfaces.owner_state import IOwnerStateRepository, OwnerStateDocument

_WRITE_ATTEMPTS = 5


class SqlOwnerStateRepository(IOwnerStateRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(self, scope: OwnerScope) -> OwnerStateDocument | None:
        stored, _seen = self._read(scope)
        return OwnerStateDocument(stored) if stored is not None else None

    def save(self, document: OwnerStateDocument, scope: OwnerScope) -> None:
        for _attempt in range(_WRITE_ATTEMPTS):
            stored, seen_body = self._read(scope)
            merged = dict(document.payload) if isinstance(document.payload, dict) else {}
            providers = (stored or {}).get("provider_state")
            if isinstance(providers, dict):
                merged["provider_state"] = providers
            if stored is not None and CLEANUP_CHECKPOINT_KEY in stored:
                merged[CLEANUP_CHECKPOINT_KEY] = stored[CLEANUP_CHECKPOINT_KEY]
            if self._write(scope, dump_json(merged), utc_now_iso(), seen_body=seen_body):
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
