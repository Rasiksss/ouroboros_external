"""The scheduler pack is an instruction skill now: the tools live in the core catalog.

What it ships is the text, so what is checked is that the text parses, that no executable
payload crept back in, and that the rules the pack exists to state are still in it.
"""

from __future__ import annotations

import pathlib

from new_core.skills.manifest import parse_skill_manifest_text

SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
SKILL_TEXT = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def _manifest():
    return parse_skill_manifest_text(SKILL_TEXT)


def test_the_manifest_is_a_valid_instruction_skill():
    manifest = _manifest()
    assert manifest.name == "tasks-scheduler"
    assert manifest.is_instruction()
    assert manifest.description.strip()
    assert manifest.when_to_use.strip()
    assert manifest.validate() == []
    # An instruction skill declares no runtime, entry, or permissions to grant.
    assert manifest.entry == ""
    assert manifest.permissions == []


def test_the_pack_carries_no_executable_payload():
    assert list(SKILL_DIR.glob("*.py")) == []


def test_the_instructions_keep_the_hard_boundary():
    body = _manifest().body
    assert "schedule_task" in body
    assert "send_user_message" in body
    assert "confirm_schedule" in body
    assert "self-contained" in body
    assert "at-most-once" in body
    # The tools are disableable, so the fail-closed rule has to survive edits to this file.
    assert "unavailable" in body


def test_the_instructions_say_how_to_reach_the_tools():
    """They are discoverable, so an instruction that skipped this leaves the model with no way in."""
    from ouroboros.tools.catalog import discoverable_tool_names

    body = _manifest().body
    assert "enable_tools" in body
    for name in ("create_reminder", "create_scheduled_task", "list_schedules", "move_schedule_here"):
        assert name in discoverable_tool_names()
        assert name in body
