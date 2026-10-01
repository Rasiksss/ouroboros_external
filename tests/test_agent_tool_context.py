"""`OuroborosAgent._build_tool_context`: the task → ToolContext field mapping.

Split out of `_prepare_task_context`, the builder is pure assembly — no IO, no
events, no registry install — so the mapping is testable on a bare agent skeleton.
"""

from __future__ import annotations

import ouroboros.agent.core as core


def _agent(tmp_path, *, chat_id=7):
    agent = object.__new__(core.OuroborosAgent)
    agent.env = core.Env(repo_dir=tmp_path, drive_root=tmp_path)
    agent._pending_events = []
    agent._current_chat_id = chat_id
    agent._current_task_type = "chat"
    agent._current_task_source = "browser"
    agent._media_senders = {}

    agent._emit_progress = lambda _text: None
    # The builder binds the events channel to this queue; a skeleton agent has none.
    agent._event_queue = None
    # ...and binds the task's usage totals into it. `build_task_run` points this at the
    # run's `accumulated_usage`; a skeleton agent outside a run owns its own dict.
    agent._task_usage = {}
    return agent


def test_build_tool_context_maps_task_fields(tmp_path):
    transport = {"source_message_id": "m-1"}
    agent = _agent(tmp_path)

    ctx = agent._build_tool_context(
        {
            "id": "task-1",
            "transport": transport,
            "conversation_id": "conv-9",
            "trace_id": "tr-1",
            "depth": 2,
            "_is_direct_chat": True,
            "attachments": [{"kind": "image"}, "not-a-dict"],
        },
        tmp_path / "ws",
        root_task_id="root-1",
    )

    assert (ctx.task_id, ctx.root_task_id) == ("task-1", "root-1")
    assert ctx.conversation_id == "conv-9"
    assert (ctx.trace_id, ctx.task_depth, ctx.is_direct_chat) == ("tr-1", 2, True)
    assert ctx.task_workspace == tmp_path / "ws"
    # session state comes off the agent, not the task
    assert (ctx.current_chat_id, ctx.current_task_type, ctx.current_task_source) == (7, "chat", "browser")
    assert ctx.pending_events is agent._pending_events
    # non-dict attachments are dropped; the transport dict is copied, not aliased
    assert ctx.attachments == [{"kind": "image"}]
    assert ctx.transport == transport and ctx.transport is not transport


def test_build_tool_context_falls_back_to_the_chat_conversation(tmp_path):
    ctx = _agent(tmp_path)._build_tool_context({"id": "task-2"}, None, root_task_id="")

    assert ctx.conversation_id == "chat:7"
    assert ctx.root_task_id is None
    assert ctx.task_workspace is None


def test_build_tool_context_without_ids_or_chat(tmp_path):
    ctx = _agent(tmp_path, chat_id=None)._build_tool_context({}, None, root_task_id="")

    # An id-less task yields None rather than "", the value tools branch on.
    assert ctx.task_id is None
    assert ctx.conversation_id == ""
    assert ctx.attachments == []
