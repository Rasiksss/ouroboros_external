from sqlalchemy.engine import Engine

from infra.sql_transaction import SqlTransactionManager
from ouroboros.storage.execution_memory.repository import SqlExecutionMemoryRepository
from ouroboros.storage.runtime_persistence import runtime_persistence


def sql_execution_memory_repository(engine: Engine | None = None) -> SqlExecutionMemoryRepository:
    return SqlExecutionMemoryRepository(
        SqlTransactionManager(engine if engine is not None else runtime_persistence.current.engine)
    )
