"""`OuroborosAgent._build_tool_context`: the task → ToolContext field mapping.

Split out of `_prepare_task_context`, the builder is pure assembly — no IO, no
events, no registry install — so the mapping is testable on a bare agent skeleton.
"""

from __future__ import annotations

from pathlib import Path

import ouroboros.agent.core as core
from new_core.identity import OwnerScope
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.files import stored_file
from tests.helpers.shared import make_empty_extension_runtime, make_empty_skill_surface

_SCOPE = OwnerScope(owner_id="global", agent_id="main", surface_id="browser", chat_id="7")


def _agent(tmp_path):
    agent = object.__new__(core.OuroborosAgent)
    agent.audit_event_repository = sql_audit_event_repository()
    agent.env = core.Env(repo_dir=tmp_path, drive_root=tmp_path)
    agent._pending_events = []
    agent._current_task_type = "chat"
    agent._outbound = None
    agent.extension_runtime = make_empty_extension_runtime()

    # The builder binds the events channel to this publisher; a skeleton agent has none.
    agent._events = None
    # ...and binds the task's usage totals into it. `build_task_run` points this at the
    # run's `accumulated_usage`; a skeleton agent outside a run owns its own dict.
    agent._task_usage = {}
    agent._last_progress_ts = 0.0
    agent._current_task_id = None
    return agent


def test_build_tool_context_maps_task_fields(tmp_path: Path) -> None:
    transport = {"source_message_id": "m-1"}
    agent = _agent(tmp_path)
    skill_surface = make_empty_skill_surface()
    attachment = stored_file(scope=_SCOPE, filename="photo.png", mime="image/png", data=b"\x89PNG\r\n\x1a\npayload")

    ctx = agent._build_tool_context(
        {
            "id": "task-1",
            "transport": transport,
            "trace_id": "tr-1",
            "depth": 2,
            "attachments": [attachment, "not-a-dict"],
        },
        tmp_path / "ws",
        root_task_id="root-1",
        scope=_SCOPE,
        skill_surface=skill_surface,
    )

    assert (ctx.task_id, ctx.root_task_id) == ("task-1", "root-1")
    assert (ctx.trace_id, ctx.task_depth) == ("tr-1", 2)
    assert ctx.task_workspace == tmp_path / "ws"
    # identity is the minted scope, whole; the task type still comes off the agent
    assert ctx.scope is _SCOPE
    assert ctx.current_task_type == "chat"
    assert ctx.pending_events is agent._pending_events
    # non-dict attachments are dropped; the transport dict is copied, not aliased
    assert ctx.attachments == [attachment]
    assert ctx.transport == transport and ctx.transport is not transport
    assert ctx.skill_surface is skill_surface
    assert ctx.extension_runtime is agent.extension_runtime


def test_build_tool_context_without_ids(tmp_path):
    ctx = _agent(tmp_path)._build_tool_context(
        {},
        None,
        root_task_id="",
        scope=_SCOPE,
        skill_surface=make_empty_skill_surface(),
    )

    # An id-less task yields None rather than "", the value tools branch on.
    assert ctx.task_id is None
    assert ctx.root_task_id is None
    assert ctx.task_workspace is None
    assert ctx.attachments == []


def test_the_context_carries_no_identity_projection_beside_the_scope(tmp_path):
    """The old projections were a second address that could disagree with the scope."""
    ctx = _agent(tmp_path)._build_tool_context(
        {"id": "t"},
        None,
        root_task_id="",
        scope=_SCOPE,
        skill_surface=make_empty_skill_surface(),
    )

    for removed in ("current_chat_id", "current_task_source", "memory_owner_id", "conversation_id"):
        assert not hasattr(ctx, removed), removed
