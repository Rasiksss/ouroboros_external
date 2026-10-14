from unittest.mock import Mock

from ouroboros.agent_runtime.cancellation import StoreCancellation
from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE


@parametrize_from_dict({"ordinary": {"detached": False}, "detached": {"detached": True}})
def test_cancellation_reads_and_writes_the_same_task_repository(detached):
    store = runtime_persistence.current
    store.tasks_repo.create_task(TaskRequest(id="task", detached=detached, scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(runtime_id="test-runtime")
    assert claimed is not None
    claim_token = str(claimed.metadata["execution_claim"]) if detached else None
    tasks = Mock(wraps=store.tasks_repo)
    signal = StoreCancellation(
        tasks_repo=tasks, task_id="task", scope=TEST_SCOPE, poll_interval_sec=0, claim_token=claim_token
    )

    assert not signal.is_stopped()
    signal.request_stop()

    assert signal.is_stopped()
    if detached:
        tasks.is_task_attempt_active.assert_called_once_with("task", claim_token=claim_token)
        tasks.read_task_stop_state.assert_not_called()
    else:
        tasks.read_task_stop_state.assert_called_once_with("task")
        tasks.is_task_attempt_active.assert_not_called()
    tasks.request_task_stop.assert_called_once_with("task", scope=TEST_SCOPE)
    assert store.tasks_repo.read_task_stop_state("task").stop_requested
