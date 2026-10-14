from sqlalchemy.engine import Engine

from infra.sql_transaction import SqlTransactionManager
from ouroboros.observability.infra.repositories.audit_events.repository import SqlAuditEventRepository
from ouroboros.storage.runtime_persistence import runtime_persistence


def sql_audit_event_repository(engine: Engine | None = None) -> SqlAuditEventRepository:
    return SqlAuditEventRepository(
        SqlTransactionManager(engine if engine is not None else runtime_persistence.current.engine)
    )
