from __future__ import annotations

import uuid
from typing import cast

from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.engine import Engine

from infra.sql_connection import sql_connection
from new_core.identity import OwnerScope
from ouroboros.storage.runtime_models import OwnerRuntimeState
from ouroboros.storage.runtime_serialization import dump_json, load_json
from ouroboros.utils import utc_now_iso
from transport.dialogs.interfaces import IDialogsCursorRepository

_WRITE_ATTEMPTS = 5


class SqlDialogsCursorRepository(IDialogsCursorRepository):
    def __init__(self, engine: Engine, *, scope: OwnerScope) -> None:
        self._engine = engine
        self._scope = scope

    def load_cursor(self) -> int:
        stored, _ = self._read()
        provider_state = stored.get("provider_state")
        if not isinstance(provider_state, dict):
            return 0
        dialogs_state = provider_state.get("dialogs")
        if not isinstance(dialogs_state, dict):
            return 0
        return int(cast(str | int | float, dialogs_state.get("seq") or 0))

    def save_cursor(self, cursor: int) -> None:
        for _attempt in range(_WRITE_ATTEMPTS):
            stored, seen_body = self._read()
            updated = stored or OwnerRuntimeState(session_id=uuid.uuid4().hex).model_dump(mode="json")
            providers = updated.get("provider_state")
            merged = dict(providers) if isinstance(providers, dict) else {}
            merged["dialogs"] = {"seq": cursor}
            updated["provider_state"] = merged
            if self._write(dump_json(updated), utc_now_iso(), seen_body=seen_body):
                return
        raise RuntimeError(
            f"owner runtime state for {self._scope.owner_id}/{self._scope.agent_id} "
            "is being rewritten faster than it can be merged"
        )

    def _read(self) -> tuple[dict[str, JsonValue], str | None]:
        with sql_connection(self._engine) as conn:
            row = conn.execute(
                text("SELECT payload_json FROM owner_runtime_state WHERE owner_id=:owner_id AND agent_id=:agent_id"),
                {"owner_id": self._scope.owner_id, "agent_id": self._scope.agent_id},
            ).fetchone()
        if row is None:
            return {}, None
        body = str(row[0])
        return load_json(body), body

    def _write(self, body: str, now: str, *, seen_body: str | None) -> bool:
        params = {
            "owner_id": self._scope.owner_id,
            "agent_id": self._scope.agent_id,
            "payload_json": body,
            "updated_at": now,
        }
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
                    params,
                ).rowcount
            else:
                applied = conn.execute(
                    text(
                        """
                        UPDATE owner_runtime_state SET payload_json=:payload_json, updated_at=:updated_at
                        WHERE owner_id=:owner_id AND agent_id=:agent_id AND payload_json=:seen_body
                        """
                    ),
                    {**params, "seen_body": seen_body},
                ).rowcount
            conn.commit()
        return bool(applied)
