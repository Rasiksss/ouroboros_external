"""Headless Tool Policy Engine authorization tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from new_core.llm.types import ToolCall, ToolCallFunction


@pytest.fixture
def tool_env_and_logs(tmp_path):
    """The ``(catalog, ctx)`` pair plus a log dir, as ``_execute_single_tool`` takes them."""
    from tests._shared import make_tool_env

    logs = tmp_path / "logs"
    logs.mkdir()
    catalog, ctx = make_tool_env(tmp_path, tmp_path)
    return catalog, ctx, logs


def _run_single_tool(catalog, ctx, tc, drive_logs, task_id):
    """Bridge for these synchronous tests: ``_execute_single_tool`` is a coroutine.

    Production reaches it through ``ToolCallExecutor.execute``, which bridges once per
    round. These tests want the single call without a round's timeout and live-log
    machinery, so they bridge here — the same shape ``tests/_shared.run_tool`` uses one
    level up.
    """
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from tests._shared import collecting_event_sink

    executor = build_tool_call_executor(catalog=catalog, drive_logs=drive_logs, events=collecting_event_sink())
    return asyncio.run(executor._execute_single_tool(ctx, tc, task_id=task_id))


def test_authorize_skill_tools_require_sandbox() -> None:
    from ouroboros.policy.tool_authorization import authorize_tool

    for tool in ("skill_exec", "skill_preflight"):
        decision = authorize_tool(tool, {})
        assert decision.outcome == "allow"
        assert decision.sandbox_required is True


def test_authorize_shell_tool_denied() -> None:
    from ouroboros.policy.tool_authorization import authorize_tool

    decision = authorize_tool("run_shell", {"command": "ls"})
    assert decision.outcome == "deny"
    assert decision.capability_class == "shell"
    assert decision.deny_reason


def test_authorize_git_tool_denied() -> None:
    from ouroboros.policy.tool_authorization import authorize_tool

    decision = authorize_tool("repo_write", {"path": "foo.py", "content": "x"})
    assert decision.outcome == "deny"
    assert decision.capability_class == "git"


def test_policy_tool_denied_audit_payload() -> None:
    from ouroboros.policy.tool_authorization import build_policy_tool_denied_event

    payload = build_policy_tool_denied_event(
        tool="run_shell",
        capability_class="shell",
        reason="excluded",
        ts="2026-06-07T00:00:00Z",
    )
    assert payload == {
        "ts": "2026-06-07T00:00:00Z",
        "tool": "run_shell",
        "capability_class": "shell",
        "reason": "excluded",
    }


def test_policy_tool_denied_audit_event(tool_env_and_logs, monkeypatch) -> None:
    """Policy deny must record policy.tool_denied before execute."""
    from new_core.tools.catalog import ToolCatalog
    from ouroboros.observability.event_record import list_runtime_events

    catalog, ctx, drive_logs = tool_env_and_logs
    events_file = drive_logs / "events.jsonl"

    async def _fail_execute(*_args, **_kwargs):
        raise AssertionError("aexecute must not run")

    monkeypatch.setattr(ToolCatalog, "aexecute", _fail_execute)

    tc = ToolCall(
        id="call-deny-audit",
        function=ToolCallFunction(name="run_shell", arguments=json.dumps({"command": "ls"})),
    )
    _run_single_tool(catalog, ctx, tc, drive_logs, "audit-t1")

    assert not events_file.exists()
    recorded = list_runtime_events(drive_logs.parent, event_type="policy.tool_denied", limit=10)
    denied = [event for event in recorded if event.get("type") == "policy.tool_denied"]
    assert len(denied) == 1
    assert denied[0]["tool"] == "run_shell"
    assert denied[0]["capability_class"] == "shell"
    assert denied[0]["log_stream"] == "events"


def test_loop_execution_denies_shell_before_dispatch(tool_env_and_logs, monkeypatch) -> None:
    """authorize_tool wiring must deny run_shell before the catalog is asked to execute."""
    from new_core.tools.catalog import ToolCatalog

    catalog, ctx, drive_logs = tool_env_and_logs
    execute_called = {"count": 0}
    original_execute = ToolCatalog.aexecute

    async def _spy_execute(self, tool_ctx, name, args):
        execute_called["count"] += 1
        return await original_execute(self, tool_ctx, name, args)

    monkeypatch.setattr(ToolCatalog, "aexecute", _spy_execute)

    tc = ToolCall(
        id="call-1",
        function=ToolCallFunction(name="run_shell", arguments=json.dumps({"command": "ls"})),
    )
    result = _run_single_tool(catalog, ctx, tc, drive_logs, "t1")

    assert execute_called["count"] == 0
    assert result["is_error"] is True
    assert "POLICY_DENIED" in result["result"]
    assert "run_shell" in result["result"]


def test_loop_execution_allows_core_read_tool(tool_env_and_logs, monkeypatch) -> None:
    from new_core.tools.catalog import ToolCatalog

    catalog, ctx, drive_logs = tool_env_and_logs

    async def _fixed_result(self, tool_ctx, name, args):
        return "ok" if name == "data_list" else "fail"

    monkeypatch.setattr(ToolCatalog, "aexecute", _fixed_result)

    tc = ToolCall(
        id="call-2",
        function=ToolCallFunction(name="data_list", arguments=json.dumps({"dir": "."})),
    )
    result = _run_single_tool(catalog, ctx, tc, drive_logs, "t2")

    assert result["is_error"] is False
    assert result["result"] == "ok"
