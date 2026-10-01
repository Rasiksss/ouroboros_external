from __future__ import annotations

import pytest

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
    assert {"/api/health", "/health/readiness", "/health/liveness"} <= paths
    assert "/api/settings" not in paths
    assert "/" not in paths
    assert "/ws" not in paths
    assert not any(path.startswith("/api/git") for path in paths)
    assert not any(path.startswith("/api/marketplace") for path in paths)


def test_chat_persistence_does_not_use_jsonl_logs() -> None:
    """Operational chat/progress must use the runtime DB, not logs/*.jsonl writes."""
    import pathlib
    import re

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    bus_src = (repo_root / "supervisor" / "message_bus.py").read_text(encoding="utf-8")
    assert "chat.jsonl" not in bus_src
    assert "progress.jsonl" not in bus_src

    pipeline_src = (repo_root / "ouroboros" / "agent" / "task_pipeline.py").read_text(encoding="utf-8")
    jsonl_writes = re.findall(r"append_jsonl\([^)]+\)", pipeline_src, flags=re.DOTALL)
    assert not any("chat.jsonl" in call or "progress.jsonl" in call for call in jsonl_writes)


def test_storage_backend_defaults_by_deploy_env(monkeypatch) -> None:
    from ouroboros.storage.blob.factory import reset_storage_provider_for_tests, resolve_storage_backend
    from ouroboros.storage.settings.bootstrap import bootstrap_config, reset_config_for_tests

    reset_config_for_tests()
    reset_storage_provider_for_tests()
    monkeypatch.delenv("OUROBOROS_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("OUROBOROS_ENV", "development")
    bootstrap_config(force=True)
    assert resolve_storage_backend() == "file"

    reset_config_for_tests()
    reset_storage_provider_for_tests()
    monkeypatch.delenv("OUROBOROS_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    bootstrap_config(force=True)
    assert resolve_storage_backend() == "s3"


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
    from tests._shared import collecting_event_sink, make_tool_env, run_tool

    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")

    assert name in excluded_tool_names()
    assert authorize_tool(name, {}).outcome == "deny"
    assert catalog.find(name) is None
    assert name not in catalog.local_entries()
    assert run_tool(catalog, ctx, name) == f"⚠️ TOOL_UNAVAILABLE: tool {name} is not available in this runtime profile."
    assert name not in CORE_TOOL_NAMES
    assert name not in discoverable_tool_names()
    assert name not in read_only_parallel_tool_names()
    assert name not in tool_result_limits()
    assert name not in untruncated_tool_names()

    events: list[tuple] = []
    # The journal is a sender interface now: the executor calls ``DriveEventRecorder``,
    # which is the only thing left that names ``record_event``.
    monkeypatch.setattr(loop_adapters, "record_event", lambda *args: events.append(args))
    tool_call = ToolCall(id=f"call-{name}", function=ToolCallFunction(name=name, arguments="{}"))
    executor = loop_adapters.build_tool_call_executor(
        catalog=catalog, drive_logs=tmp_path / "logs", events=collecting_event_sink()
    )
    results = executor.execute(
        [tool_call],
        ctx=ctx,
        task_id=f"task-{name}",
    )

    assert results[0]["is_error"] is True
    assert results[0]["result"].startswith(f"⚠️ POLICY_DENIED ({name})")
    assert any(event[2] == "policy.tool_denied" and event[3]["tool"] == name for event in events)


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
        decision = authorize_tool(name, {})
        assert decision.outcome == "deny", name
        assert decision.deny_reason
