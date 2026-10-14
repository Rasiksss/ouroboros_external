from __future__ import annotations

from new_core.identity import ServiceJob
from ouroboros.contracts.owner_scope import service_identity
from ouroboros.storage.runtime_persistence import runtime_persistence
from transport.dialogs.infra.repositories.repo import SqlDialogsCursorRepository


def sql_dialogs_cursor_repository() -> SqlDialogsCursorRepository:
    return SqlDialogsCursorRepository(
        runtime_persistence.current.engine, scope=service_identity(job=ServiceJob.DIALOGS_CURSOR).as_scope()
    )
