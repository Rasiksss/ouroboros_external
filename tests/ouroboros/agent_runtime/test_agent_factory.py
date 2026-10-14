from unittest.mock import Mock

from ouroboros.contracts.owner_scope import ServiceJob, service_identity
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.interfaces.owner_state import OwnerStateDocument
from tests.helpers.agent_owner_session import sql_owner_session_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.executor import make_executor
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.startup_usage import sql_startup_usage_repository
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


def test_executor_preserves_state_readers_when_rebuilding_the_agent_factory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("ouroboros.agent.core._worker_boot_logged", False)
    repository = sql_owner_session_repository()
    sql_owner_state_repository().save(OwnerStateDocument({"session_id": "session"}), TEST_SCOPE)
    startup_usage = Mock(wraps=sql_startup_usage_repository())
    invocations = sql_execution_memory_repository()
    confirmations = sql_tool_confirmation_reader()
    executor = make_executor(
        tmp_path,
        runtime_persistence.current,
        session_repository=repository,
        startup_usage_repository=startup_usage,
        execution_memory_repository=invocations,
        tool_confirmation_reader=confirmations,
    )

    try:
        before_start = executor.new_agent()
        executor.ensure_started()
        after_start = executor.new_agent()
        executor.shutdown()
        executor.ensure_started()
        after_restart = executor.new_agent()

        assert before_start is not after_start and after_start is not after_restart
        for agent in (before_start, after_start, after_restart):
            assert agent.session_repository is repository
            assert agent.startup_usage_repository is startup_usage
            assert agent.execution_memory_repository is invocations
            assert agent.tool_confirmation_reader is confirmations
            assert agent.session_repository.get_session_id(TEST_SCOPE) == "session"
        startup_usage.get_usage.assert_called_once_with(service_identity(job=ServiceJob.STARTUP).as_scope())
    finally:
        executor.shutdown()
