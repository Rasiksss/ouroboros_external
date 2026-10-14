from __future__ import annotations

from dataclasses import replace

import pytest

from ouroboros.agent.loop_adapters import build_tool_call_executor
from ouroboros.agent.task_graph import bind_tool_surface
from ouroboros.tools.dispatch import execute_tool
from ouroboros.tools.tool_discovery import _enable_tools, _list_available_tools
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.loop import tool_runner
from tests.helpers.shared import (
    collecting_event_sink,
    make_static_tool_env,
    make_tool_call,
    make_tool_entry,
    make_tool_env,
)


def test_skill_access_filters_initial_discovery_enablement_and_restore(tmp_path):
    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    hidden = replace(ctx, skill_origins=frozenset())
    admitted = replace(ctx, skill_origins=frozenset({"skill:image-generation"}))
    schemas, active = bind_tool_surface(catalog, hidden, [])
    bind_tool_surface(catalog, admitted, [])

    assert "image_generate" not in {schema.name for schema in schemas} | active
    assert "image_generate" not in _list_available_tools(hidden)
    assert "Enabled: image_generate" not in _enable_tools(hidden, tools="image_generate")
    assert hidden.tool_surface.drain_pending_enables() == []
    assert "image_generate" in _list_available_tools(admitted)
    assert _enable_tools(admitted, tools="image_generate") == "✅ Enabled: image_generate"
    restored = tool_runner(tmp_path, catalog=catalog).restore_surface(hidden, {"image_generate", "data_read"})
    assert [schema.name for schema in restored] == ["data_read"]


@pytest.mark.anyio
async def test_dispatch_refuses_skill_tools_without_execution_or_catalog_leaks(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    ran: list[str] = []
    catalog, ctx = make_static_tool_env(
        tmp_path, make_tool_entry("image_generate", lambda ctx, **kw: ran.append("ran") or "image")
    )
    hidden = replace(ctx, skill_origins=frozenset())

    assert "TOOL_UNAVAILABLE" in await execute_tool(catalog, hidden, "image_generate", {}, origin="builtin")
    assert ran == []
    assert await execute_tool(catalog, hidden, "missing", {}, origin="builtin") == "⚠️ Unknown tool: missing."
    admitted = replace(ctx, skill_origins=frozenset({"skill:image-generation"}))
    monkeypatch.setenv("OUROBOROS_DISABLED_TOOLS", "image_generate")
    assert "TOOL_UNAVAILABLE" in await execute_tool(catalog, admitted, "image_generate", {}, origin="builtin")
    assert ran == []


@pytest.mark.parametrize("active", [{"file_write"}, set()], ids=["active", "inactive"])
def test_skill_denial_precedes_confirmation(tmp_path, active):
    ran: list[str] = []
    catalog, ctx = make_static_tool_env(
        tmp_path, make_tool_entry("file_write", lambda ctx, **kw: ran.append("ran") or "written")
    )
    ctx.skill_origins = frozenset()
    executor = build_tool_call_executor(
        catalog=catalog,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
        events=collecting_event_sink(),
        repeat_guard=None,
        confirmation_gate=True,
    )
    call = make_tool_call("file_write", {"path": "notes.txt"})

    assert executor.gated_calls([call], ctx=ctx, active_tool_names=active) == []
    results = executor.execute([call], ctx=ctx, task_id="task1", active_tool_names=active, decisions={})
    assert "TOOL_UNAVAILABLE" in results[0]["result"]
    assert ran == []
