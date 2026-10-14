from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from infra.sql_transaction import SqlTransactionManager
from ouroboros.observability.infra.repositories.audit_events.repository import SqlAuditEventRepository
from ouroboros.storage.runtime_models import ToolConfirmation
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.tool_confirmation.repository import SqlToolConfirmationRepository


def sql_tool_confirmation_reader(engine: Engine | None = None) -> SqlToolConfirmationRepository:
    from ouroboros.storage.settings.store import load_settings

    transaction_manager = SqlTransactionManager(engine if engine is not None else runtime_persistence.current.engine)
    return SqlToolConfirmationRepository(
        transaction_manager,
        audit_event_repository=SqlAuditEventRepository(transaction_manager),
        max_running_root_jobs_per_owner=load_settings().OUROBOROS_MAX_RUNNING_ROOT_JOBS_PER_OWNER,
    )


def seed_tool_confirmations(rows: Iterable[tuple[ToolConfirmation, str]]) -> SqlToolConfirmationRepository:
    engine = runtime_persistence.current.engine
    with engine.begin() as conn:
        for item, applied_at in rows:
            values = {**item.model_dump(), "applied_at": applied_at}
            columns = ", ".join(values)
            binds = ", ".join(f":{key}" for key in values)
            conn.execute(text(f"INSERT INTO tool_confirmations ({columns}) VALUES ({binds})"), values)
    return sql_tool_confirmation_reader(engine)
