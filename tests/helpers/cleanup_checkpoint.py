from sqlalchemy.engine import Engine

from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.infra.repositories.cleanup_checkpoint.repo import SqlCleanupCheckpointRepository


def sql_cleanup_checkpoint_repository(engine: Engine | None = None) -> SqlCleanupCheckpointRepository:
    return SqlCleanupCheckpointRepository(engine if engine is not None else runtime_persistence.current.engine)
