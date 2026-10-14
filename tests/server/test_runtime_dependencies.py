from types import SimpleNamespace
from unittest.mock import Mock, call

import server
from supervisor.queue import TaskQueue
from supervisor.state import RuntimeStateRepository
from tests.helpers.shared import TEST_SCOPE


def test_repeated_status_reads_reuse_injected_repositories():
    queue = Mock(spec=TaskQueue)
    queue.list_pending_records.return_value = [object(), object()]
    queue.list_running.return_value = [object()]
    state = Mock(spec=RuntimeStateRepository)
    state.for_scope.return_value.load.side_effect = [SimpleNamespace(spent_calls=7), SimpleNamespace(spent_calls=8)]

    results = [server._runtime_status(TEST_SCOPE, task_queue=queue, state_repo=state) for _ in range(2)]

    assert [item["spent_calls"] for item in results] == [7, 8]
    assert all(item["pending_count"] == 2 and item["running_count"] == 1 for item in results)
    assert state.for_scope.call_args_list == [call(TEST_SCOPE), call(TEST_SCOPE)]
    assert queue.list_pending_records.call_args_list == [call(scope=TEST_SCOPE), call(scope=TEST_SCOPE)]
