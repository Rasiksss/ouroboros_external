"""Universal tool kill switch via OUROBOROS_DISABLED_TOOLS.

A tool named in the flag must be invisible to the agent (absent from the first
prompt and from discovery), impossible to enable, and blocked at execution — for
core tools (gigasearch) and discoverable tools (calculator) alike.

The registry that these behaviours once lived on is gone; the same four surfaces
are now the catalog's first-prompt selection (``bind_tool_surface``), the two
discovery meta-tools (``_list_available_tools`` / ``_enable_tools``), and the
dispatch gate (``execute_tool``). This exercises them through the production
wiring in ``tests/_shared`` rather than a registry façade.
"""

from __future__ import annotations

import pathlib

import pytest

from ouroboros.agent.task_graph import bind_tool_surface
from ouroboros.storage.settings.store import reset_memory_settings_for_tests
from ouroboros.tools.catalog import disabled_tool_names, spec_for_tool
from ouroboros.tools.tool_discovery import _enable_tools, _list_available_tools
from tests._shared import make_tool_env, run_tool


def _env(tmp_path: pathlib.Path):
    """Catalog + context wired like the server lifespan, over the test's roots."""
    return make_tool_env(tmp_path / "repo", tmp_path / "data")


def _bind(tmp_path: pathlib.Path):
    """First-prompt surface: schema names plus a context whose ``tool_surface`` is set."""
    catalog, ctx = _env(tmp_path)
    schemas, _active = bind_tool_surface(catalog, ctx, messages=[])
    return catalog, ctx, {schema.name for schema in schemas}


@pytest.fixture()
def disabled(monkeypatch: pytest.MonkeyPatch):
    """Disable gigasearch (core) and calculator (discoverable) via the flag."""

    def _set(value: str) -> None:
        monkeypatch.setenv("OUROBOROS_DISABLED_TOOLS", value)
        reset_memory_settings_for_tests()

    reset_memory_settings_for_tests()
    yield _set
    reset_memory_settings_for_tests()


def test_disabled_tool_names_parses_comma_and_space(disabled) -> None:
    disabled("gigasearch, calculator  browse_page")
    assert disabled_tool_names() == frozenset({"gigasearch", "calculator", "browse_page"})


def test_spec_for_tool_forces_excluded_surface(disabled) -> None:
    disabled("gigasearch")
    spec = spec_for_tool("gigasearch")
    assert spec.surface == "excluded"
    # Unrelated fields are preserved (only the surface is overridden).
    assert spec.category == "network"
    assert spec.result_limit == 30_000


def test_disabled_core_tool_absent_from_first_prompt(disabled, tmp_path: pathlib.Path) -> None:
    disabled("gigasearch")
    _catalog, _ctx, names = _bind(tmp_path)
    assert "gigasearch" not in names


def test_disabled_discoverable_tool_absent_from_first_prompt(disabled, tmp_path: pathlib.Path) -> None:
    # A discoverable tool is never on the first prompt anyway; disabling it keeps it off.
    disabled("calculator")
    _catalog, _ctx, names = _bind(tmp_path)
    assert "calculator" not in names


def test_disabled_tool_not_discoverable(disabled, tmp_path: pathlib.Path) -> None:
    disabled("calculator")
    _catalog, ctx, _names = _bind(tmp_path)
    listing = _list_available_tools(ctx)
    assert "calculator" not in listing


def test_disabled_tool_cannot_be_enabled(disabled, tmp_path: pathlib.Path) -> None:
    disabled("gigasearch")
    _catalog, ctx, _names = _bind(tmp_path)
    result = _enable_tools(ctx, tools="gigasearch")
    assert "❌ Not found: gigasearch" in result
    assert "✅ Enabled" not in result


def test_disabled_tool_execution_blocked(disabled, tmp_path: pathlib.Path) -> None:
    disabled("gigasearch")
    catalog, ctx = _env(tmp_path)
    out = run_tool(catalog, ctx, "gigasearch", {"query": "anything"})
    assert out.startswith("⚠️ TOOL_UNAVAILABLE")


def test_absent_flag_keeps_tool_visible(disabled, tmp_path: pathlib.Path) -> None:
    disabled("")  # empty flag disables nothing
    assert disabled_tool_names() == frozenset()
    assert spec_for_tool("gigasearch").surface == "core"
    _catalog, _ctx, names = _bind(tmp_path)
    assert "gigasearch" in names
