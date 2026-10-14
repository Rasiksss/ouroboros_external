"""Headless Tool Policy Engine authorization tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from new_core.identity import OwnerScope
from new_core.llm.types import ToolCall, ToolCallFunction
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.egress_auth import null_egress_auth_dependencies
from tests.helpers.execution_memory import sql_execution_memory_repository


class _McpServerIdsReader:
    def __init__(self, server_ids: list[str]) -> None:
        self._server_ids = server_ids

    def __call__(self, scope: OwnerScope) -> list[str]:
        del scope
        return list(self._server_ids)


@pytest.fixture
def tool_env_and_logs(tmp_path):
    """The ``(catalog, ctx)`` pair plus a log dir, as the tool executor takes them."""
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from tests.helpers.shared import make_tool_env

    logs = tmp_path / "logs"
    logs.mkdir()
    # The audit trail these tests read lives in the runtime store, and the store is composed
    # rather than conjured on first use: this is the test's composition root.
    runtime_persistence.initialize()
    catalog, ctx = make_tool_env(tmp_path, tmp_path)
    return catalog, ctx, logs


def _run_single_tool(catalog, ctx, tc, drive_logs, task_id, *, audit_event_repository):
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from tests.helpers.shared import collecting_event_sink

    executor = build_tool_call_executor(
        confirmation_gate=False,
        catalog=catalog,
        events=collecting_event_sink(),
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=audit_event_repository,
    )
    [result] = executor.execute([tc], ctx=ctx, task_id=task_id, decisions={})
    return result


def test_authorize_skill_tools_require_sandbox() -> None:
    from ouroboros.policy.tool_authorization import authorize_tool

    for tool in ("skill_exec",):
        decision = authorize_tool(tool, confirmation_gate=False)
        assert decision.outcome == "allow"
        assert decision.sandbox_required is True


def test_authorize_shell_tool_denied() -> None:
    from ouroboros.policy.tool_authorization import authorize_tool

    decision = authorize_tool("run_shell", confirmation_gate=False)
    assert decision.outcome == "deny"
    assert decision.capability_class == "shell"
    assert decision.deny_reason


def test_authorize_git_tool_denied() -> None:
    from ouroboros.policy.tool_authorization import authorize_tool

    decision = authorize_tool("repo_write", confirmation_gate=False)
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
    audit = sql_audit_event_repository()
    _run_single_tool(catalog, ctx, tc, drive_logs, "audit-t1", audit_event_repository=audit)

    assert not events_file.exists()
    recorded = list_runtime_events(
        event_type="policy.tool_denied",
        limit=10,
        audit_event_repository=audit,
    )
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
    result = _run_single_tool(catalog, ctx, tc, drive_logs, "t1", audit_event_repository=sql_audit_event_repository())

    assert execute_called["count"] == 0
    assert result["is_error"] is True
    assert "POLICY_DENIED" in result["result"]
    assert "run_shell" in result["result"]


def test_loop_execution_refuses_mcp_tool_outside_scope(tool_env_and_logs) -> None:
    """A scoped MCP projection denies an unprompted name before the manager call."""
    from new_core.tools.providers import McpToolProvider

    class _Manager:
        def __init__(self) -> None:
            self.calls = 0

        def is_available(self):
            return True

        def discovered_tools(self):
            return []

        def resolve(self, name):
            return ("blocked", "search") if name == "mcp__blocked__search" else None

        def call_tool_sync(self, *_args):
            self.calls += 1
            return "unexpected"

    catalog, ctx, drive_logs = tool_env_and_logs
    manager = _Manager()
    catalog.initialize(
        [
            McpToolProvider(
                servers=[SimpleNamespace(id="allowed"), SimpleNamespace(id="blocked")],
                server_ids_reader=_McpServerIdsReader(["allowed"]),
                auth_dependencies=null_egress_auth_dependencies(),
                manager=manager,
            )
        ],
    )

    result = _run_single_tool(
        catalog,
        ctx,
        ToolCall(id="call-mcp-deny", function=ToolCallFunction(name="mcp__blocked__search", arguments="{}")),
        drive_logs,
        "mcp-deny",
        audit_event_repository=sql_audit_event_repository(),
    )

    assert manager.calls == 0
    assert result["result"].startswith("⚠️ POLICY_DENIED")


def test_later_mcp_refusal_cannot_veto_the_first_provider(tool_env_and_logs) -> None:
    from typing import Any

    from new_core.tools.interfaces import IToolProvider, ToolDescriptor
    from new_core.tools.providers import McpToolProvider
    from new_core.tools.runtimes import INLINE_RUNTIME
    from new_core.tools.schema import ToolSchema

    name = "mcp__blocked__search"

    class _FirstProvider(IToolProvider):
        default_runtime = INLINE_RUNTIME

        def refresh(self):
            return None

        def descriptors(self, scope):
            return [self.get(name, scope)]

        def get(self, requested, scope):
            if requested != name:
                return None
            return ToolDescriptor(
                schema=ToolSchema(name=name, description="first", parameters={}),
                timeout_sec=1,
                origin="builtin",
                surface="core",
                capability_class="read_only",
                safety_policy="skip",
            )

        def refuse_dispatch(self, requested, scope):
            return None

        def execute(self, ctx: Any, requested: str, args: dict[str, Any]) -> str:
            return "first provider"

    class _Manager:
        def is_available(self):
            return True

        def resolve(self, requested):
            return ("blocked", "search") if requested == name else None

    catalog, ctx, drive_logs = tool_env_and_logs
    catalog.initialize(
        [
            _FirstProvider(),
            McpToolProvider(
                servers=[SimpleNamespace(id="blocked")],
                server_ids_reader=_McpServerIdsReader([]),
                auth_dependencies=null_egress_auth_dependencies(),
                manager=_Manager(),
            ),
        ],
    )

    result = _run_single_tool(
        catalog,
        ctx,
        ToolCall(id="call-collision", function=ToolCallFunction(name=name, arguments="{}")),
        drive_logs,
        "collision",
        audit_event_repository=sql_audit_event_repository(),
    )

    assert result["result"] == "first provider"


def test_loop_preserves_a_non_policy_provider_refusal_once(tool_env_and_logs) -> None:
    from typing import Any

    from new_core.tools.interfaces import IToolProvider

    name = "ext_4_demo_ping"

    class _WithholdingProvider(IToolProvider):
        def __init__(self) -> None:
            self.refusals = 0

        def refresh(self):
            return None

        def descriptors(self, scope):
            return []

        def get(self, requested, scope):
            return None

        def refuse_dispatch(self, requested, scope):
            if requested != name:
                return None
            self.refusals += 1
            return "⚠️ EXTENSION_NOT_LIVE: demo"

        def execute(self, ctx: Any, requested: str, args: dict[str, Any]) -> str:
            raise AssertionError("withheld provider must not execute")

    catalog, ctx, drive_logs = tool_env_and_logs
    provider = _WithholdingProvider()
    catalog.initialize([provider])

    result = _run_single_tool(
        catalog,
        ctx,
        ToolCall(id="call-extension", function=ToolCallFunction(name=name, arguments="{}")),
        drive_logs,
        "extension-refusal",
        audit_event_repository=sql_audit_event_repository(),
    )

    assert result["result"] == "⚠️ EXTENSION_NOT_LIVE: demo"
    assert provider.refusals == 1


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
    result = _run_single_tool(catalog, ctx, tc, drive_logs, "t2", audit_event_repository=sql_audit_event_repository())

    assert result["is_error"] is False
    assert result["result"] == "ok"


def test_a_listed_tool_needs_the_owner_s_decision() -> None:
    from ouroboros.policy.tool_authorization import authorize_tool

    decision = authorize_tool("data_write", confirmation_gate=True)
    assert decision.outcome == "confirm_pending"


def test_a_listed_origin_covers_a_tool_with_a_generated_name() -> None:
    from ouroboros.policy.tool_authorization import authorize_tool

    decision = authorize_tool("delegate_to_research", origin="a2a:research", confirmation_gate=True)
    assert decision.outcome == "confirm_pending"


def test_an_unlisted_tool_is_authorized_as_before() -> None:
    from ouroboros.policy.tool_authorization import authorize_tool

    assert authorize_tool("data_list", confirmation_gate=True).outcome == "allow"
    assert authorize_tool("send_file", confirmation_gate=True).outcome == "allow"
    assert authorize_tool("run_shell", confirmation_gate=True).outcome == "deny"


def test_a_listed_tool_is_allowed_while_the_gate_is_off() -> None:
    from ouroboros.policy.tool_authorization import authorize_tool

    decision = authorize_tool("data_write", confirmation_gate=False)
    assert decision.outcome == "allow"


def test_no_gated_tool_is_parallel_safe() -> None:
    """One open request per scope: two gated calls at once in a task would expire each other's."""
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata
    from security_config.confirmations import confirmation_required_tools
    from tests.helpers.shared import TEST_SCOPE

    for name in sorted(confirmation_required_tools):
        assert resolve_tool_runtime_metadata(name, scope=TEST_SCOPE).parallel_safe is False, name
