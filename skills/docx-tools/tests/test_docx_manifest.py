"""The pack is a loadable script skill and declares exactly the scripts it ships."""

from __future__ import annotations

import pathlib

from new_core.skills.manifest import parse_skill_manifest_text

SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]


def test_the_manifest_is_valid_and_names_the_shipped_scripts() -> None:
    manifest = parse_skill_manifest_text((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))

    assert manifest.name == SKILL_DIR.name
    assert manifest.is_script()
    assert manifest.validate() == []
    assert manifest.when_to_use
    declared = {entry["name"] for entry in manifest.scripts}
    assert declared == {"make_docx.py", "read_docx.py"}
    assert all((SKILL_DIR / "scripts" / name).is_file() for name in declared)
