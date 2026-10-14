from types import SimpleNamespace

from ouroboros.agent.loop_adapters import ExecutionMemoryInvocationRecorder
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import TEST_SCOPE


def test_recorder_writes_to_the_injected_repository(tmp_path):
    repository = sql_execution_memory_repository()
    recorder = ExecutionMemoryInvocationRecorder(repository)

    recorder.record(
        SimpleNamespace(drive_root=tmp_path, task_id="task", scope=TEST_SCOPE),
        {"tool_call_id": "call", "fn_name": "calculator", "result": "2", "tool_args": {}},
    )

    assert [(item.task_id, item.tool_invocation_id) for item in repository.list_tool_invocations(task_id="task")] == [
        ("task", "call")
    ]
