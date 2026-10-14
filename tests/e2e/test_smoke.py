"""Smoke test suite for Ouroboros.

Tests core invariants:
- All modules import cleanly
- Tool registry discovers all expected tools
- Utility functions work correctly
- Memory operations don't crash

Run: python -m pytest tests/e2e/test_smoke.py -v
"""

import pathlib
import sys
import tempfile

import pytest

from ouroboros.policy.capabilities import CORE_TOOL_NAMES
from ouroboros.storage.core_memory.scratchpad import get_scratchpad
from tests.helpers.shared import TEST_SCOPE, make_tool_env, run_tool

# ── Module imports ───────────────────────────────────────────────

CORE_MODULES = [
    "ouroboros.agent",
    "ouroboros.agent.context",
    "ouroboros.agent.loop_adapters",
    "new_core.loop.transcript_merge",
    "ouroboros.llm",
    "ouroboros.storage.core_memory.memory",
    "ouroboros.utils",
    "ouroboros.policy.capabilities",
]

TOOL_MODULES = [
    "ouroboros.tools.attachments",
    "ouroboros.tools.calculator",
    "ouroboros.tools.calendar_resolve",
    "ouroboros.tools.core",
    "ouroboros.tools.gigasearch",
    "ouroboros.tools.service.messaging",
    "ouroboros.tools.service.memory",
    "ouroboros.tools.service.orchestration",
    "ouroboros.tools.recent_tasks",
    "ouroboros.tools.scheduling",
    "ouroboros.tools.skills",
    "ouroboros.tools.skill_exec",
]

SUPERVISOR_MODULES = [
    "supervisor.state",
    "supervisor.message_bus",
    "supervisor.queue",
    "ouroboros.agent_runtime.executor",
    "supervisor.events",
]


@pytest.mark.parametrize("module", CORE_MODULES + TOOL_MODULES + SUPERVISOR_MODULES)
def test_import(module):
    """Every module imports without error."""
    __import__(module)


# ── Tool registry ────────────────────────────────────────────────


@pytest.fixture
def _tool_env():
    tmp = pathlib.Path(tempfile.mkdtemp())
    return make_tool_env(tmp, tmp)


@pytest.fixture
def catalog(_tool_env):
    return _tool_env[0]


@pytest.fixture
def ctx(_tool_env):
    return _tool_env[1]


def test_tool_set_matches(catalog, ctx):
    """Tool registry contains exactly the expected tools (no more, no less)."""
    schemas = [d.schema for d in catalog.descriptors(TEST_SCOPE) if d.name in CORE_TOOL_NAMES]
    actual_tools = {t.name for t in schemas}
    expected_tools = set(EXPECTED_TOOLS)

    assert actual_tools == expected_tools


EXPECTED_TOOLS = [
    "data_read",
    "data_write",
    "data_list",
    "gigasearch",
    "calendar_resolve",
    "chat_history",
    "attachment_read",
    "recent_tasks",
    "update_scratchpad",
    "calendar_resolve",
    "send_user_message",
    "send_photo",
    "send_image",
    "send_file",
    "list_available_tools",
    "enable_tools",
    "schedule_task",
    "cancel_task",
    "get_task_result",
    "wait_for_task",
    "memory_save",
    "memory_list",
    "memory_read",
    "memory_delete",
    "list_skills",
    "check_skills",
    "skill_read",
]


def test_unknown_tool_returns_warning(catalog, ctx):
    """Calling unknown tool returns warning, not exception."""
    result = run_tool(catalog, ctx, "__nonexistent__", {})
    assert "Unknown tool" in result or "⚠️" in result


def test_tool_schemas_valid(catalog, ctx):
    """All tool schemas have required OpenAI fields."""
    for schema in [d.schema for d in catalog.descriptors(TEST_SCOPE) if d.name in CORE_TOOL_NAMES]:
        assert schema.name
        assert isinstance(schema.description, str)
        params = schema.parameters
        assert params["type"] == "object"
        assert "properties" in params
        assert schema.to_dict()["type"] == "function"


def test_tool_execute_basic(catalog, ctx):
    """Actually execute a simple tool to verify execution works."""
    data_file = ctx.drive_root / "hello.txt"
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_text("hello from data_read", encoding="utf-8")

    result = run_tool(catalog, ctx, "data_read", {"path": "hello.txt"})

    assert isinstance(result, str), "Tool execute should return string"
    assert "hello from data_read" in result


def test_frozen_registry_includes_packaged_tool_modules(monkeypatch):
    """Frozen-mode registry must still load packaged tool modules."""

    tmp = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    catalog, _ctx = make_tool_env(tmp, tmp)
    available = {t.name for t in [d.schema for d in catalog.descriptors(TEST_SCOPE) if d.name in CORE_TOOL_NAMES]}
    expected_subset = {
        "data_read",
        "data_list",
    }
    missing = expected_subset - available
    assert missing == set(), f"Frozen registry missing tools: {sorted(missing)}"


# ── Utilities ────────────────────────────────────────────────────


def test_clip_text():
    from ouroboros.utils import clip_text

    # Test 1: Long text gets clipped (max_chars=500)
    long_text = "hello world " * 100  # ~1200 chars
    result = clip_text(long_text, 500)
    assert len(result) < len(long_text), "Long text should be clipped"
    assert len(result) > 0, "Result should not be empty"
    assert "...(truncated)..." in result, "Truncation marker should be present"

    # Test 2: Short text passes through unchanged
    short_text = "hello world"
    result_short = clip_text(short_text, 500)
    assert result_short == short_text, "Short text should pass through unchanged"


def test_estimate_tokens():
    from ouroboros.utils import estimate_tokens

    tokens = estimate_tokens("Hello world, this is a test.")
    assert 5 <= tokens <= 20


# ── Memory ───────────────────────────────────────────────────────


def test_memory_chat_history_empty():
    """Chat history returns string when no data."""
    from ouroboros.storage.chat_store import render_chat_history

    with tempfile.TemporaryDirectory() as tmp:
        from new_core.identity import ServiceJob
        from ouroboros.contracts.owner_scope import service_identity

        scope = service_identity(job=ServiceJob.STARTUP).as_scope()
        history = render_chat_history(pathlib.Path(tmp), scope=scope, count=10)
        assert isinstance(history, str)


def test_memory_persistence():
    """Scratchpad persists across service instances (write with one, read with another)."""
    get_scratchpad().append_block(TEST_SCOPE, "test persistence content")

    get_scratchpad.cache_clear()
    content = get_scratchpad().render(TEST_SCOPE)
    assert "test persistence content" in content, "Scratchpad should persist across instances"


@pytest.mark.parametrize("tool_name", EXPECTED_TOOLS)
def test_tool_registered(catalog, ctx, tool_name):
    """Each expected tool is in the registry."""
    available = [t.name for t in [d.schema for d in catalog.descriptors(TEST_SCOPE) if d.name in CORE_TOOL_NAMES]]
    assert tool_name in available, f"{tool_name} not in registry"


def test_safe_relpath_normal():
    from ouroboros.utils import safe_relpath

    result = safe_relpath("foo/bar.py")
    assert result == "foo/bar.py"


def test_safe_relpath_rejects_traversal():
    from ouroboros.utils import safe_relpath

    with pytest.raises(ValueError):
        safe_relpath("../../../etc/passwd")


def test_safe_relpath_strips_leading_slash():
    """safe_relpath strips leading / but doesn't raise."""
    from ouroboros.utils import safe_relpath

    result = safe_relpath("/etc/passwd")
    assert not result.startswith("/")


def test_memory_scratchpad():
    """Scratchpad appends and renders without crash."""
    from ouroboros.storage.core_memory.scratchpad import get_scratchpad

    get_scratchpad().append_block(TEST_SCOPE, "test content")
    assert "test content" in get_scratchpad().render(TEST_SCOPE)


@pytest.fixture(autouse=True)
def _reset_core_memory_services() -> None:
    """Drop the cached service so this test binds to its own runtime DB."""
    get_scratchpad.cache_clear()
