from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ouroboros.agent.task_graph import bind_tool_surface
from ouroboros.tools.catalog import WORKSTATION_TOOL_NAMES
from tests.helpers.shared import make_tool_env, run_tool


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("OUROBOROS_ENABLED_TOOLS", raising=False)
    for name in WORKSTATION_TOOL_NAMES:
        monkeypatch.delenv(f"TOOL_{name.upper()}_ENABLED", raising=False)


@pytest.fixture
def _ws_configured(monkeypatch):
    monkeypatch.setenv("WORKSTATION_BASE_URL", "https://workstation.test")


def test_workstation_session_adds_all_six_tools(tmp_path, _ws_configured) -> None:
    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    ctx.workstation = MagicMock()

    _schemas, active = bind_tool_surface(catalog, ctx, [])

    assert active >= WORKSTATION_TOOL_NAMES


def test_no_session_means_none_of_the_six_are_present(tmp_path) -> None:
    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")

    _schemas, active = bind_tool_surface(catalog, ctx, [])

    assert active.isdisjoint(WORKSTATION_TOOL_NAMES)


def test_configured_enabled_tools_cannot_smuggle_them_in_without_a_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_ENABLED_TOOLS", "file_read,code_exec")
    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")

    _schemas, active = bind_tool_surface(catalog, ctx, [])

    assert active.isdisjoint(WORKSTATION_TOOL_NAMES)


def test_dispatch_without_a_session_returns_the_guard_text(tmp_path, _ws_configured) -> None:
    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    bind_tool_surface(catalog, ctx, [])

    result = run_tool(catalog, ctx, "file_read", {"path": "a.txt"})

    assert "TOOL_UNAVAILABLE" in result
