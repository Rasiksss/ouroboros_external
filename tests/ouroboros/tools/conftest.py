"""Infrastructure fixtures for tool tests."""

from __future__ import annotations

import pytest

from tests.helpers.shared import make_tool_ctx


@pytest.fixture
def tool_ctx(tmp_path):
    """A ToolContext with repo_dir and drive_root set to tmp_path."""
    return make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)
