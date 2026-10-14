"""Agent factory helpers extracted from test_task_graph."""

from __future__ import annotations

import queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import ouroboros.agent.core as core
from new_core.loop import build_task_graph
from new_core.tools.interfaces import NullToolEvents
from tests.helpers.agent_owner_session import sql_owner_session_repository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import make_empty_extension_runtime, make_empty_skill_snapshot, make_skill_availability
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


def make_agent(tmp_path):
    agent = object.__new__(core.OuroborosAgent)
    agent.session_repository = sql_owner_session_repository()
    agent.execution_memory_repository = sql_execution_memory_repository()
    agent.audit_event_repository = sql_audit_event_repository()
    agent.tool_confirmation_reader = sql_tool_confirmation_reader()
    agent.env = core.Env(repo_dir=tmp_path, drive_root=tmp_path)
    agent.skill_snapshot = make_empty_skill_snapshot()
    agent.skill_availability = make_skill_availability()
    agent.extension_runtime = make_empty_extension_runtime()
    agent._pending_events = []
    agent._events = None
    agent._current_task_type = None
    agent._current_task_id = None
    agent._run_state = None
    agent._task_usage = {}
    agent._outbound = None
    agent._incoming_messages = queue.Queue()
    agent._last_progress_ts = 0.0
    agent._task_started_ts = 0.0
    agent.llm = MagicMock()
    agent.llm.default_model.return_value = "m1"
    tool_ctx = SimpleNamespace(
        _pending_compaction=None,
        pending_skill_read=None,
        projected_skills=set(),
        events=NullToolEvents(),
        task_id=None,
        messages=None,
        tool_surface=None,
    )
    agent._task_graph = build_task_graph()
    return agent, tool_ctx
