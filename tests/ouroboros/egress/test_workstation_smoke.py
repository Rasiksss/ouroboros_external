"""End to end against a real AEF WorkStation instance — opt-in only.

Skipped unless ``WORKSTATION_BASE_URL`` is actually set in the environment
(manual dev/staging verification, not CI — no fake transport here, unlike
``tests/ouroboros/egress/test_a2a_egress_smoke.py``: there is no local reference server for
this protocol to fake against). Everything from settings through the tool
handlers is real; only the WorkStation instance on the other end is not
something this repo controls.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from new_core.tools.context import ToolContext
from ouroboros.egress.workstation.client import WorkstationConfig
from ouroboros.egress.workstation.manager import WorkstationManager
from ouroboros.storage.settings.schema import AppSettings
from ouroboros.tools.workstation import apply_patch_tool, edit_tool, exec_tool, read_tool, write_tool
from tests.helpers.shared import TEST_SCOPE

pytestmark = pytest.mark.integration


def _ctx() -> ToolContext:
    base_url = os.environ.get("WORKSTATION_BASE_URL", "").strip()
    if not base_url:
        pytest.skip("WORKSTATION_BASE_URL not set; this smoke test needs a real WorkStation instance")
    settings = AppSettings(
        WORKSTATION_BASE_URL=base_url,
        WORKSTATION_CREATE_SECRET=os.environ.get("WORKSTATION_CREATE_SECRET", ""),
    )
    config = WorkstationConfig.from_settings(settings)
    assert config is not None
    manager = WorkstationManager()
    handle = manager.get_or_create(scope_key="workstation-smoke-test", config=config)
    return ToolContext(
        repo_dir=pathlib.Path("."),
        drive_root=pathlib.Path("."),
        scope=TEST_SCOPE,
        skill_surface=None,
        workstation=handle,
    )


def test_write_exec_edit_read_round_trip() -> None:
    ctx = _ctx()

    write_result = write_tool._handle_file_write(ctx, path="smoke.txt", content="hello\n")
    assert write_result.startswith("OK"), write_result

    exec_result = exec_tool._handle_code_exec(ctx, command="cat smoke.txt")
    assert "hello" in exec_result, exec_result

    edit_result = edit_tool._handle_file_edit(
        ctx, path="smoke.txt", edits=[{"oldText": "hello", "newText": "hello world"}]
    )
    assert edit_result.startswith("OK"), edit_result

    read_result = read_tool._handle_file_read(ctx, path="smoke.txt")
    assert "hello world" in read_result, read_result

    patch = "*** Begin Patch\n*** Add File: smoke2.txt\n+from apply_patch\n*** End Patch"
    patch_result = apply_patch_tool._handle_apply_patch(ctx, input=patch)
    assert "1 added" in patch_result, patch_result

    read_result_2 = read_tool._handle_file_read(ctx, path="smoke2.txt")
    assert "from apply_patch" in read_result_2, read_result_2
