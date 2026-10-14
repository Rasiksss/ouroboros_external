import time

import pytest

from infra.sql_connection import store_deadline
from infra.sql_transaction import SqlTransactionManager
from ouroboros.observability.infra.repositories.audit_events.repository import SqlAuditEventRepository
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.tool_confirmation.repository import SqlToolConfirmationRepository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.runtime_store import claim_task, make_tool_confirmation
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.tool_confirmation_conformance import ToolConfirmationReaderConformance
from tests.helpers.tool_confirmation_write_conformance import ToolConfirmationWriteConformance
from tests.helpers.tool_confirmations import seed_tool_confirmations, sql_tool_confirmation_reader


class TestSqlToolConfirmationRepository(ToolConfirmationReaderConformance):
    def make_reader(self, rows):
        return seed_tool_confirmations(rows)


@parametrize_from_dict(
    {"status": {"method": "status"}, "open": {"method": "open"}, "decisions": {"method": "decisions"}}
)
def test_reads_respect_an_expired_deadline(method):
    reader = sql_tool_confirmation_reader()
    token = store_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            if method == "status":
                reader.read_tool_confirmation_status("id")
            elif method == "open":
                reader.read_open_tool_confirmation(TEST_SCOPE)
            else:
                reader.read_tool_confirmation_decisions("task", round_idx=1)
    finally:
        store_deadline.reset(token)


def test_parking_and_audit_roll_back_with_outer_transaction():
    store = runtime_persistence.current
    claim_task(store)
    transaction_manager = SqlTransactionManager(store.engine)
    audit = SqlAuditEventRepository(transaction_manager)
    repository = SqlToolConfirmationRepository(transaction_manager, audit_event_repository=audit)
    with pytest.raises(RuntimeError, match="abort use case"):
        with repository.transaction:
            request = repository.park_and_open(
                task_id="task1",
                claim_token="",
                confirmation=make_tool_confirmation(),
                snapshot={"round_idx": 1},
                workspace="workspace",
                arguments={},
            )
            assert request is not None
            assert repository.read_open_tool_confirmation(TEST_SCOPE) == request
            assert len(audit.list_audit_events(event_type="policy.tool_confirmation_requested")) == 1
            raise RuntimeError("abort use case")
    assert store.tasks_repo.get_task("task1").status == "running"
    assert repository.read_open_tool_confirmation(TEST_SCOPE) is None
    assert audit.list_audit_events(event_type="policy.tool_confirmation_requested") == []


class TestSqlToolConfirmationWrites(ToolConfirmationWriteConformance):
    def make_repository(self):
        claim_task(runtime_persistence.current)
        return sql_tool_confirmation_reader()
