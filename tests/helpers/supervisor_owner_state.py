from sqlalchemy.engine import Engine

from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.infra.repositories.owner_state.repo import SqlOwnerStateRepository


def sql_owner_state_repository(engine: Engine | None = None) -> SqlOwnerStateRepository:
    return SqlOwnerStateRepository(engine if engine is not None else runtime_persistence.current.engine)
