from sqlalchemy.engine import Engine

from ouroboros.agent.infra.repositories.startup_usage.repo import SqlStartupUsageRepository
from ouroboros.storage.runtime_persistence import runtime_persistence


def sql_startup_usage_repository(engine: Engine | None = None) -> SqlStartupUsageRepository:
    return SqlStartupUsageRepository(engine if engine is not None else runtime_persistence.current.engine)
