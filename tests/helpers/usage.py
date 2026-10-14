from __future__ import annotations

from sqlalchemy.engine import Engine

from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.infra.repositories.usage.repo import SqlUsageRepository


def sql_usage_repository(engine: Engine | None = None) -> SqlUsageRepository:
    return SqlUsageRepository(engine if engine is not None else runtime_persistence.current.engine)
