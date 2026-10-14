from sqlalchemy.engine import Engine

from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.infra.repositories.runtime_sessions.repo import SqlRuntimeSessionRepository


def sql_runtime_session_repository(engine: Engine | None = None) -> SqlRuntimeSessionRepository:
    return SqlRuntimeSessionRepository(engine if engine is not None else runtime_persistence.current.engine)
