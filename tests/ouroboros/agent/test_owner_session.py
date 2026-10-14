from ouroboros.agent.core import _runtime_session_id
from supervisor.interfaces.owner_state import OwnerStateDocument
from tests.helpers.agent_owner_session import sql_owner_session_repository
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.sql_faults import fail_sql
from tests.helpers.supervisor_owner_state import sql_owner_state_repository


def test_trace_session_is_read_from_the_injected_repository() -> None:
    repository = sql_owner_session_repository()
    sql_owner_state_repository().save(OwnerStateDocument({"session_id": "session"}), TEST_SCOPE)

    assert _runtime_session_id(TEST_SCOPE, repository) == "session"
    assert repository.get_session_id(TEST_SCOPE) == "session"


def test_trace_session_read_failure_keeps_the_empty_fallback() -> None:
    repository = sql_owner_session_repository()
    with fail_sql("owner_runtime_state", OSError("database unavailable")):
        assert _runtime_session_id(TEST_SCOPE, repository) == ""
