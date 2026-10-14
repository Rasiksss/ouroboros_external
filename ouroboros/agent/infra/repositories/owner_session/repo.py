from sqlalchemy import text
from sqlalchemy.engine import Engine

from infra.sql_connection import sql_connection
from new_core.identity import OwnerScope
from ouroboros.agent.interfaces.owner_session import IOwnerSessionRepository
from ouroboros.storage.runtime_serialization import load_json


class SqlOwnerSessionRepository(IOwnerSessionRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_session_id(self, scope: OwnerScope) -> str:
        with sql_connection(self._engine) as conn:
            row = conn.execute(
                text("SELECT payload_json FROM owner_runtime_state WHERE owner_id=:owner_id AND agent_id=:agent_id"),
                {"owner_id": scope.owner_id, "agent_id": scope.agent_id},
            ).fetchone()
        if row is None:
            return ""
        return str(load_json(str(row[0])).get("session_id") or "")
