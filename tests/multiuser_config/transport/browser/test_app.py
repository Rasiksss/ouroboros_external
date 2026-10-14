from multiuser_config.transport.browser.app import chat_session_id
from ouroboros.contracts.owner_scope import ServiceJob, service_identity
from supervisor.interfaces.owner_state import OwnerStateDocument
from tests.helpers.browser_owner_session import sql_owner_session_repository
from tests.helpers.sql_faults import fail_sql
from tests.helpers.supervisor_owner_state import sql_owner_state_repository


def test_chat_session_is_read_from_the_injected_repository() -> None:
    scope = service_identity(job=ServiceJob.JOURNAL_SWEEP).as_scope()
    repository = sql_owner_session_repository()
    sql_owner_state_repository().save(OwnerStateDocument({"session_id": "browser-session"}), scope)

    assert chat_session_id(repository) == "browser-session"
    assert repository.get_session_id(scope) == "browser-session"


def test_missing_chat_session_stays_empty() -> None:
    scope = service_identity(job=ServiceJob.JOURNAL_SWEEP).as_scope()
    repository = sql_owner_session_repository()

    assert chat_session_id(repository) == ""
    assert repository.get_session_id(scope) == ""
    assert sql_owner_state_repository().load(scope) is None


def test_chat_session_read_failure_keeps_the_empty_fallback(caplog) -> None:
    repository = sql_owner_session_repository()
    with fail_sql("owner_runtime_state", OSError("database unavailable")):
        assert chat_session_id(repository) == ""
        assert "Failed to read the chat session id" in caplog.text
