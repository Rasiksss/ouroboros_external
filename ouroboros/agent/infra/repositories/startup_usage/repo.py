from sqlalchemy import text
from sqlalchemy.engine import Engine

from infra.sql_connection import sql_connection
from new_core.identity import OwnerScope
from ouroboros.agent.interfaces.startup_usage import IStartupUsageRepository, StartupUsage
from ouroboros.storage.runtime_serialization import load_json


class SqlStartupUsageRepository(IStartupUsageRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_usage(self, scope: OwnerScope) -> StartupUsage:
        with sql_connection(self._engine) as conn:
            row = conn.execute(
                text("SELECT payload_json FROM owner_runtime_state WHERE owner_id=:owner_id AND agent_id=:agent_id"),
                {"owner_id": scope.owner_id, "agent_id": scope.agent_id},
            ).fetchone()
        return StartupUsage.model_validate(load_json(str(row[0])) if row is not None else {})
