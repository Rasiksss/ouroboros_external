"""The workstation skill is a loadable instruction skill, and it documents the tools it must reference.

An instruction skill has no code to test: what it ships is the text.  So what is checked is
that the text parses as a manifest, that the body names every workstation tool, and that the
session-lifecycle rules and error-handling guidance are actually present.
"""

from __future__ import annotations

import pathlib

from new_core.skills.manifest import parse_skill_manifest_text

SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
SKILL_TEXT = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def _manifest():
    return parse_skill_manifest_text(SKILL_TEXT)


def test_manifest_is_valid_and_loadable():
    manifest = _manifest()
    assert manifest.name == "workstation"
    assert manifest.is_instruction()
    assert manifest.description.strip()
    assert manifest.when_to_use.strip()
    assert manifest.validate() == []


def test_manifest_version_is_set():
    manifest = _manifest()
    assert manifest.version == "1.0.0"


def test_body_names_all_six_tools():
    body = _manifest().body
    for tool in (
        "workstation_connect",
        "code_exec",
        "file_read",
        "file_write",
        "file_edit",
        "apply_patch",
    ):
        assert tool in body, f"body must reference the {tool} tool"


def test_body_requires_connect_before_other_tools():
    body = _manifest().body
    connect_pos = body.index("workstation_connect")
    code_exec_pos = body.index("code_exec")
    assert connect_pos < code_exec_pos


def test_body_documents_session_scoping():
    body = _manifest().body
    assert "conversation" in body.lower() or "chat" in body.lower()


def test_body_documents_tool_unavailable():
    body = _manifest().body
    assert "TOOL_UNAVAILABLE" in body


def test_body_documents_workstation_unavailable():
    body = _manifest().body
    assert "WORKSTATION_UNAVAILABLE" in body


def test_body_documents_access_denied():
    body = _manifest().body
    assert "ACCESS_DENIED" in body


def test_body_documents_timeout_handling():
    body = _manifest().body
    assert "timeout" in body.lower()


def test_body_documents_file_edit_old_text_new_text_pattern():
    body = _manifest().body
    assert "oldText" in body
    assert "newText" in body


def test_body_documents_apply_patch_format():
    body = _manifest().body
    assert "Begin Patch" in body
    assert "End Patch" in body
