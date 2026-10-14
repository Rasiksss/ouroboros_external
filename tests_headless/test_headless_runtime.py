from __future__ import annotations

import pytest

from new_core.identity import OwnerScope
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository

TEST_SCOPE = OwnerScope(owner_id="global", agent_id="main", surface_id="browser", chat_id="1")

REMOVED_SELF_REVIEW_TOOLS = (
    "repo_read",
    "repo_list",
    "code_search",
    "codebase_digest",
    "codebase_health",
)


def test_gigachat_defaults_are_headless_only() -> None:
    from ouroboros.app.config import SETTINGS_DEFAULTS

    assert str(SETTINGS_DEFAULTS["OUROBOROS_MODEL"]).startswith("gigachat-b2bank::")
    dev_only_keys = (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
    )
    for key in dev_only_keys:
        assert not str(SETTINGS_DEFAULTS.get(key, "") or "").strip()


def test_deploy_env_production_and_development(monkeypatch) -> None:
    from ouroboros.app.config import get_deploy_env, is_development, is_production
    from ouroboros.storage.settings.bootstrap import bootstrap_config, reset_config_for_tests

    reset_config_for_tests()
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    bootstrap_config(force=True)
    assert get_deploy_env() == "production"
    assert is_production()
    assert not is_development()

    reset_config_for_tests()
    monkeypatch.setenv("OUROBOROS_ENV", "development")
    bootstrap_config(force=True)
    assert get_deploy_env() == "development"
    assert is_development()
    assert not is_production()


def test_headless_router_exposes_minimal_routes() -> None:
    from ouroboros.gateway.router import collect_routes

    paths = {getattr(route, "path", "") for route in collect_routes(data_dir=__import__("pathlib").Path("."))}
    assert {"/api/health", "/start", "/ready", "/live"} <= paths
    assert "/api/settings" not in paths
    assert "/" not in paths
    assert "/ws" not in paths
    assert not any(path.startswith("/api/git") for path in paths)
    assert not any(path.startswith("/api/marketplace") for path in paths)
    # The registry is mounted beside this collection, never inside it: in the multi-user
    # mode it belongs to a tier of its own, and a collection that carried it would make the
    # transport tier serve another tier's surface.
    assert not any(path.startswith("/api/agents") for path in paths)


def test_the_only_owner_bearing_surfaces_the_full_runtime_adds_are_the_admitted_three() -> None:
    """The admitted surfaces, named rather than merely described.

    ``docs/INVARIANTS.md`` §5 keeps the default router minimal. The full runtime mounts three
    things beside it — the agent registry, the owner settings writer and the chat registry —
    and this is where that admission is asserted, so a fourth owner-bearing route group cannot
    arrive as an unremarked mount.
    """
    import pathlib

    import server

    mounted = {getattr(route, "path", ""): route.app for route in server.routes if type(route).__name__ == "Mount"}
    assert set(mounted) == {"/api"}
    groups = {"/" + path.split("/")[1] for path in mounted["/api"].openapi()["paths"]}
    assert groups == {"/agents", "/owner-settings", "/chats"}

    invariants = (pathlib.Path(server.__file__).parent / "docs" / "INVARIANTS.md").read_text(encoding="utf-8")
    assert "/api/agents" in invariants
    assert "/api/owner-settings" in invariants
    assert "/api/chats" in invariants


def test_chat_persistence_does_not_use_jsonl_logs() -> None:
    """Operational chat/progress must use the runtime DB, not logs/*.jsonl writes."""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    bus_src = (repo_root / "supervisor" / "message_bus.py").read_text(encoding="utf-8")
    assert "chat.jsonl" not in bus_src
    assert "progress.jsonl" not in bus_src

    pipeline_src = (repo_root / "ouroboros" / "agent" / "task_pipeline.py").read_text(encoding="utf-8")
    assert "chat.jsonl" not in pipeline_src
    assert "progress.jsonl" not in pipeline_src


def test_tool_surface_has_no_self_modification_tools() -> None:
    from ouroboros.policy.capabilities import CORE_TOOL_NAMES, META_TOOL_NAMES, REVIEWED_MUTATIVE_TOOLS

    forbidden = {
        "repo_write",
        "repo_commit",
        "str_replace_editor",
        "run_shell",
        "git_status",
        "git_diff",
        "rollback_to_target",
        "plan_task",
        "toggle_evolution",
    }
    assert forbidden.isdisjoint(CORE_TOOL_NAMES)
    assert not META_TOOL_NAMES
    assert not REVIEWED_MUTATIVE_TOOLS


@pytest.mark.parametrize("name", REMOVED_SELF_REVIEW_TOOLS)
def test_removed_self_review_tools_are_policy_tombstones_only(tmp_path, monkeypatch, name: str) -> None:
    """The name survives as policy metadata; nothing behind it does.

    Two halves, and both matter. The catalog must not resolve the name — no provider
    offers a descriptor, so there is nothing to dispatch even if a gate let it through —
    and the authorization gate must deny it, so a stale model call is refused before the
    safety supervisor is paid for.
    """
    import ouroboros.agent.loop_adapters as loop_adapters
    from new_core.llm.types import ToolCall, ToolCallFunction
    from ouroboros.policy.capabilities import CORE_TOOL_NAMES
    from ouroboros.policy.tool_authorization import authorize_tool
    from ouroboros.tools.catalog import (
        discoverable_tool_names,
        excluded_tool_names,
        read_only_parallel_tool_names,
        tool_result_limits,
        untruncated_tool_names,
    )
    from tests.helpers.shared import collecting_event_sink, make_tool_env, run_tool

    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")

    assert name in excluded_tool_names()
    assert authorize_tool(name, confirmation_gate=False).outcome == "deny"
    assert catalog.find(name, TEST_SCOPE) is None
    assert name not in catalog.local_entries()
    assert run_tool(catalog, ctx, name) == f"⚠️ TOOL_UNAVAILABLE: tool {name} is not available in this runtime profile."
    assert name not in CORE_TOOL_NAMES
    assert name not in discoverable_tool_names()
    assert name not in read_only_parallel_tool_names()
    assert name not in tool_result_limits()
    assert name not in untruncated_tool_names()

    audit = sql_audit_event_repository()
    tool_call = ToolCall(id=f"call-{name}", function=ToolCallFunction(name=name, arguments="{}"))
    executor = loop_adapters.build_tool_call_executor(
        confirmation_gate=False,
        catalog=catalog,
        events=collecting_event_sink(),
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=audit,
    )
    results = executor.execute(
        [tool_call],
        ctx=ctx,
        task_id=f"task-{name}",
        decisions={},
    )

    assert results[0]["is_error"] is True
    assert results[0]["result"].startswith(f"⚠️ POLICY_DENIED ({name})")
    assert any(
        event.event_type == "policy.tool_denied" and event.metadata["tool"] == name
        for event in audit.list_audit_events()
    )


def test_excluded_tools_denied_by_authorize_tool() -> None:
    """Excluded tools (run_shell, git helpers) remain denied after authorize_tool wiring."""
    from ouroboros.policy.tool_authorization import authorize_tool

    forbidden = {
        "run_shell",
        "repo_write",
        "repo_commit",
        "str_replace_editor",
        "git_status",
        "git_diff",
        "rollback_to_target",
        "plan_task",
        "toggle_evolution",
    }
    for name in forbidden:
        decision = authorize_tool(name, confirmation_gate=False)
        assert decision.outcome == "deny", name
        assert decision.deny_reason
