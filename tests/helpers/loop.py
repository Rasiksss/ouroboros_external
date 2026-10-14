"""Loop adapter test helpers extracted from test_loop_graph_adapters."""

from __future__ import annotations

from unittest.mock import MagicMock

from ouroboros.agent.loop_adapters import LLMProviderAdapter, ToolRunnerAdapter, build_tool_call_executor
from ouroboros.agent.task_ledger import RepeatToolGuard, TaskLedger
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import TEST_SCOPE, FakeCancellation, collecting_event_sink, make_catalog


def _events():
    return collecting_event_sink()


def llm_provider(tmp_path, **over):
    base = {
        "llm": MagicMock(),
        "drive_logs": tmp_path,
        "task_id": "t",
        "task_type": "task",
        "events": None,
        "max_retries": 3,
        "fallback_model": "",
        "fallback_use_local": False,
        "scope": TEST_SCOPE,
        "middlewares": [],
        "cancellation": FakeCancellation(),
    }
    base.update(over)
    return LLMProviderAdapter(**base, audit_event_repository=sql_audit_event_repository())


def tool_runner(tmp_path=None, *, drive_root=None, repeat_guard=None, catalog=None, events=None, cancellation=None):
    return ToolRunnerAdapter(
        executor=build_tool_call_executor(
            catalog=catalog if catalog is not None else make_catalog([]),
            events=events if events is not None else _events(),
            confirmation_gate=False,
            task_ledger=TaskLedger(),
            repeat_guard=repeat_guard or RepeatToolGuard(),
            execution_memory_repository=sql_execution_memory_repository(),
            audit_event_repository=sql_audit_event_repository(),
        ),
        drive_root=drive_root,
        cancellation=cancellation or FakeCancellation(),
    )
