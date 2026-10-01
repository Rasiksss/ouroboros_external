"""Tests for prompt cache layout and determinism."""

import pathlib
import tempfile

import pytest

from new_core.tools.schema import ToolSchema
from ouroboros.storage.core_memory.memory import get_memory
from ouroboros.storage.core_memory.scratchpad import get_scratchpad


@pytest.fixture(autouse=True)
def _reset_core_memory_services() -> None:
    """Drop the cached services so this test binds to its own runtime DB.

    Context assembly reads both, so these tests reach the cached factories even
    when they never name them.
    """
    get_memory.cache_clear()
    get_scratchpad.cache_clear()


def _make_env(tmpdir: pathlib.Path):
    from ouroboros.agent import Env

    repo_dir = tmpdir / "repo"
    drive_root = tmpdir / "drive"
    repo_dir.mkdir(parents=True, exist_ok=True)
    drive_root.mkdir(parents=True, exist_ok=True)
    for subdir in ["drive/state", "drive/memory", "drive/memory/knowledge", "drive/logs", "repo/docs", "repo/prompts"]:
        (tmpdir / subdir).mkdir(parents=True, exist_ok=True)
    (repo_dir / "prompts" / "SYSTEM.md").write_text("You are Ouroboros.", encoding="utf-8")
    (repo_dir / "docs" / "ARCHITECTURE.md").write_text("# Ouroboros v1.2.3 — Architecture", encoding="utf-8")
    (repo_dir / "docs" / "DEVELOPMENT.md").write_text("# DEVELOPMENT.md", encoding="utf-8")
    (repo_dir / "README.md").write_text("version-1.2.3", encoding="utf-8")
    (repo_dir / "docs" / "CHECKLISTS.md").write_text("## Repo Commit Checklist", encoding="utf-8")
    from tests.helpers.owner_state import seed_owner_state

    seed_owner_state(drive_root, spent_calls=0)
    (drive_root / "memory" / "scratchpad.md").write_text("scratch", encoding="utf-8")
    (drive_root / "memory" / "identity.md").write_text("identity", encoding="utf-8")
    return Env(repo_dir=repo_dir, drive_root=drive_root)


def _blocks(env, **kwargs):
    from ouroboros.agent.context import build_llm_messages

    messages, _ = build_llm_messages(env=env, task={"id": "t1", "type": "task", "text": "hi"}, **kwargs)
    system_msg = messages[0]
    assert system_msg["role"] == "system"
    assert isinstance(system_msg["content"], list)
    return system_msg["content"]


def test_the_cached_prefix_runs_base_prompt_then_bodies_then_index():
    """Invalidation is prefix-based, so the volatile thing has to come last."""
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    env = _make_env(tmpdir)
    get_memory().save("project_x", "project", "Project context", "body")
    get_memory().save("git-recipes", "knowledge", "Rebase workflow", "body")

    blocks = _blocks(env)
    texts = [block.get("text", "") for block in blocks]

    assert len(blocks) == 4
    assert texts[0].startswith("You are Ouroboros")
    assert texts[1].startswith("## Identity (")
    assert texts[2].startswith("## Memory (")
    assert "## Knowledge (" in texts[2]
    assert texts[3].lstrip().startswith("## Scratchpad")

    assert blocks[0]["cache_control"]["ttl"] == "1h"
    assert "cache_control" in blocks[1]
    assert "cache_control" in blocks[2]
    assert "cache_control" not in blocks[3]


def test_no_more_than_four_cache_breakpoints_are_declared():
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    env = _make_env(tmpdir)
    get_memory().save("project_x", "project", "Project context", "body")

    breakpoints = sum(1 for block in _blocks(env) if "cache_control" in block)

    assert breakpoints <= 4


def test_an_empty_index_block_is_omitted_rather_than_declared_empty():
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    env = _make_env(tmpdir)

    texts = [block.get("text", "") for block in _blocks(env)]

    assert not any(text.startswith("## Memory (") for text in texts)
    assert not any(text.startswith("## Knowledge (") for text in texts)


def test_production_and_development_assemble_the_same_memory_sections(monkeypatch):
    """One builder: the memory sections must not depend on the deploy environment."""
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    env = _make_env(tmpdir)
    get_memory().save("project_x", "project", "Project context", "body")
    get_memory().save("git-recipes", "knowledge", "Rebase workflow", "knowledge body")

    def _memory_blocks(server_profile: bool) -> list[str]:
        monkeypatch.setattr("ouroboros.app.config.is_server_profile", lambda: server_profile)
        return [
            block.get("text", "")
            for block in _blocks(env)
            if block.get("text", "").startswith(("## Identity (", "## Memory (", "## Pinned memory:"))
        ]

    production = _memory_blocks(True)
    development = _memory_blocks(False)

    assert production == development
    assert any("## Knowledge (" in text for text in production)


def test_production_declares_the_base_body_and_index_breakpoints(monkeypatch):
    monkeypatch.setattr("ouroboros.app.config.is_server_profile", lambda: True)
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    env = _make_env(tmpdir)
    get_memory().save("git-recipes", "knowledge", "Rebase workflow", "knowledge body")

    blocks = _blocks(env)
    cached = [block.get("text", "") for block in blocks if "cache_control" in block]

    assert len(cached) == 3
    assert cached[0].startswith("You are Ouroboros")
    assert cached[1].startswith("## Identity (")
    assert cached[2].startswith("## Memory (") or cached[2].startswith("## Knowledge (")
    assert "cache_control" not in blocks[-1]


def test_sanitize_chat_completion_tools_sorts_by_name():
    from ouroboros.llm import LLMClient

    tools = [
        ToolSchema(name="zeta_tool", description="z", parameters={"type": "object", "properties": {}}),
        ToolSchema(name="alpha_tool", description="a", parameters={"type": "object", "properties": {}}),
    ]

    sanitized = LLMClient._sanitize_chat_completion_tools(tools)
    assert [tool.name for tool in sanitized] == ["alpha_tool", "zeta_tool"]


def test_sanitize_chat_completion_tools_deduplicates_before_sorting():
    from ouroboros.llm import LLMClient

    tools = [
        ToolSchema(name="beta_tool", description="first", parameters={"type": "object", "properties": {}}),
        ToolSchema(name="alpha_tool", description="alpha", parameters={"type": "object", "properties": {}}),
        ToolSchema(name="beta_tool", description="second", parameters={"type": "object", "properties": {}}),
    ]

    sanitized = LLMClient._sanitize_chat_completion_tools(tools)
    assert [tool.name for tool in sanitized] == ["alpha_tool", "beta_tool"]
    assert sanitized[1].description == "first"


def test_sanitize_chat_completion_tools_drops_provider_invalid_names():
    from ouroboros.llm import LLMClient

    tools = [
        ToolSchema(name="ext.weather.fetch", description="bad", parameters={}),
        ToolSchema(name="ext_9_r_weather_fetch", description="ok", parameters={}),
    ]

    sanitized = LLMClient._sanitize_chat_completion_tools(tools)
    assert [tool.name for tool in sanitized] == ["ext_9_r_weather_fetch"]


def test_sanitize_chat_completion_tools_drops_overlong_names():
    from ouroboros.llm import LLMClient

    tools = [
        ToolSchema(name="a" * 65, description="bad", parameters={}),
        ToolSchema(name="a" * 64, description="ok", parameters={}),
    ]

    sanitized = LLMClient._sanitize_chat_completion_tools(tools)
    assert [tool.name for tool in sanitized] == ["a" * 64]


def test_memory_sections_split_by_cache_lifetime_not_by_partition_argument():
    """The partition argument is gone: which block a section lands in is the split."""
    import ouroboros.agent.context as context_mod
    from ouroboros.agent.context import (
        build_body_memory_sections,
        build_memory_index_sections,
        build_scratchpad_sections,
    )

    tmpdir = pathlib.Path(tempfile.mkdtemp())
    env = _make_env(tmpdir)
    (tmpdir / "drive" / "memory" / "registry.md").write_text(
        "### source-a\n- **path:** memory/registry.md\n- **updated:** 2026-04-13T10:00:00+00:00\n- **gaps:** none\n",
        encoding="utf-8",
    )
    get_memory().save("project_x", "project", "Project context", "body")

    bodies = build_body_memory_sections()
    index = build_memory_index_sections()
    scratchpad = build_scratchpad_sections()
    prompt_text = "\n\n".join(part.get("text", "") for part in _blocks(env) if isinstance(part, dict))

    assert any(section.startswith("## Identity") for section in bodies)
    assert not any(section.startswith("## Scratchpad") for section in bodies)
    assert any(section.startswith("## Memory (") for section in index)
    assert any(section.startswith("## Scratchpad") for section in scratchpad)
    assert not hasattr(context_mod, "build_memory_sections")
    assert not hasattr(context_mod, "build_knowledge_sections")
    assert not hasattr(context_mod, "_build_server_profile_messages")
    assert not hasattr(context_mod, "_build_registry_digest")
    assert "## Memory Registry" not in prompt_text


def test_llm_round_event_exposes_cache_hit_rate(tmp_path):
    from new_core.llm.types import LLMMessage
    from ouroboros.agent.loop_llm_call import call_llm_with_retry
    from ouroboros.observability.event_record import list_runtime_events

    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True, exist_ok=True)

    class _CacheReportingLLM:
        def chat(self, **kwargs):
            return {"content": "ok"}, {
                "provider": "openrouter",
                "resolved_model": "anthropic/claude-sonnet-4.6",
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "cached_tokens": 750,
                "cache_write_tokens": 0,
            }

    usage = {}
    msg = call_llm_with_retry(
        _CacheReportingLLM(),
        [{"role": "user", "content": "hi"}],
        "anthropic/claude-sonnet-4.6",
        None,
        "medium",
        1,
        drive_logs,
        "task-cache",
        1,
        None,
        usage,
        "task",
        False,
    )

    assert msg == LLMMessage(content="ok")
    events = list_runtime_events(tmp_path, event_type="llm_round", limit=10)
    llm_round = next(event for event in events if event.get("type") == "llm_round")
    assert not (drive_logs / "events.jsonl").exists()
    assert llm_round["cache_hit_rate"] == 0.75


def test_llm_round_event_zero_prompt_tokens_reports_zero_hit_rate(tmp_path):
    from ouroboros.agent.loop_llm_call import call_llm_with_retry
    from ouroboros.observability.event_record import list_runtime_events

    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True, exist_ok=True)

    class _ZeroPromptLLM:
        def chat(self, **kwargs):
            return {"content": "ok"}, {
                "provider": "openrouter",
                "resolved_model": "anthropic/claude-sonnet-4.6",
                "prompt_tokens": 0,
                "completion_tokens": 10,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
            }

    usage = {}
    call_llm_with_retry(
        _ZeroPromptLLM(),
        [{"role": "user", "content": "hi"}],
        "anthropic/claude-sonnet-4.6",
        None,
        "medium",
        1,
        drive_logs,
        "task-cache-zero",
        1,
        None,
        usage,
        "task",
        False,
    )

    events = list_runtime_events(tmp_path, event_type="llm_round", limit=10)
    llm_round = next(event for event in events if event.get("type") == "llm_round")
    assert not (drive_logs / "events.jsonl").exists()
    assert llm_round["cache_hit_rate"] == 0.0
