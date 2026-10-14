from sqlalchemy.engine import Engine

from infra.sql_transaction import SqlTransactionManager
from ouroboros.storage.conversations.repository import SqlConversationRepository
from ouroboros.storage.runtime_persistence import RuntimePersistence, compose_runtime_persistence, runtime_persistence


def sql_conversation_repository(engine: Engine | None = None) -> SqlConversationRepository:
    return SqlConversationRepository(
        SqlTransactionManager(engine if engine is not None else runtime_persistence.current.engine)
    )


def make_sql_runtime_persistence(
    engine: Engine, *, db_url: str, db_urls: list[str] | None = None
) -> RuntimePersistence:
    return compose_runtime_persistence(engine, db_urls=list(db_urls or [db_url]))
