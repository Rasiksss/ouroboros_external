from sqlalchemy.engine import Engine

from ouroboros.app.infra.repositories.deployment_keyspace.repo import SqlDeploymentKeyspaceRepository
from ouroboros.storage.runtime_persistence import runtime_persistence


def sql_deployment_keyspace_repository(engine: Engine | None = None) -> SqlDeploymentKeyspaceRepository:
    return SqlDeploymentKeyspaceRepository(engine if engine is not None else runtime_persistence.current.engine)
