from sqlalchemy.engine import Engine

from ouroboros.storage.runtime_persistence import runtime_persistence
from transport.browser.infra.repositories.owner_session.repo import SqlOwnerSessionRepository


def sql_owner_session_repository(engine: Engine | None = None) -> SqlOwnerSessionRepository:
    return SqlOwnerSessionRepository(engine if engine is not None else runtime_persistence.current.engine)
