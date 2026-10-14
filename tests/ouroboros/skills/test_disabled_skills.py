"""Skill kill switch via OUROBOROS_DISABLED_SKILLS.

A skill named in the flag is absent from discovery, so it neither reaches the agent's skill
list nor contributes tools; a name that matches nothing is ignored.
"""

from __future__ import annotations

import pathlib

import pytest

from ouroboros.skills.extensions import loader as extension_loader
from ouroboros.skills.loader import disabled_skill_ids
from ouroboros.storage.settings.store import load_settings, reset_memory_settings_for_tests
from tests.helpers.fake_gateway import FakeGateway, RecordingMemoryEvents
from tests.helpers.shared import (
    METADATA_EXTENSION,
    AllowAllSkillPermissionChecker,
    clean_extension_runtime_state,
    discover_test_skills as discover_skills,
    load_metadata_extension,
    make_skill_surface,
)
from transport.skill_bridge import SkillBridge

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


@pytest.fixture()
def disabled(monkeypatch: pytest.MonkeyPatch):
    def _set(value: str) -> None:
        monkeypatch.setenv("OUROBOROS_DISABLED_SKILLS", value)
        reset_memory_settings_for_tests()

    reset_memory_settings_for_tests()
    clean_extension_runtime_state()
    yield _set
    clean_extension_runtime_state()
    reset_memory_settings_for_tests()


def _names(workspace_root: pathlib.Path, drive_root: pathlib.Path) -> list[str]:
    return [skill.name for skill in discover_skills(drive_root, workspace_root=workspace_root)]


def test_disabled_skill_ids_parse_comma_and_space(disabled) -> None:
    disabled("distribution:calendar-orientation, distribution:tasks-scheduler  distribution:docx-tools")
    assert disabled_skill_ids(load_settings()) == frozenset(
        {"distribution:calendar-orientation", "distribution:tasks-scheduler", "distribution:docx-tools"}
    )


def test_disabled_skill_absent_from_discovery(disabled, tmp_path) -> None:
    for name in ("calendar-orientation", "tasks-scheduler"):
        package = tmp_path / "skills" / name
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Example\ntype: instruction\n---\n")
    disabled("distribution:calendar-orientation")
    names = _names(tmp_path, tmp_path / "data")
    assert "calendar-orientation" not in names
    assert "tasks-scheduler" in names


def test_disabled_extension_is_unloaded_on_reload(disabled, tmp_path) -> None:
    _surface_name, drive_root, catalog = load_metadata_extension(tmp_path)
    assert METADATA_EXTENSION in extension_loader.snapshot()["extensions"]

    disabled(f"distribution:{METADATA_EXTENSION}")
    bridge = SkillBridge(
        drive_root,
        gateway=FakeGateway(),
        session_id="sess-test",
        memory_events=RecordingMemoryEvents(),
        permission_checker=AllowAllSkillPermissionChecker(),
    )
    del catalog
    skill_surface = make_skill_surface(repo_dir=tmp_path, drive_root=drive_root)
    extension_loader.reload_all(drive_root, dict, skill_bridge=bridge, skill_surface=skill_surface)
    assert extension_loader.snapshot() == {"extensions": [], "tools": []}


def test_legacy_unqualified_disabled_skill_fails_with_migration_hint(disabled, tmp_path) -> None:
    disabled("no-such-skill")

    with pytest.raises(ValueError, match=r"distribution:no-such-skill"):
        _names(REPO_ROOT, tmp_path / "data")
