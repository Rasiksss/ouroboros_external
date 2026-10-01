"""Skill loader tests without review state."""

from __future__ import annotations

import pathlib

import pytest

from ouroboros.skills.loader import (
    LoadedSkill,
    check_skills,
    compute_content_hash,
    discover_skills,
    find_skill,
    grant_status_for_skill,
    save_enabled,
    save_skill_grants,
    summarize_skills,
)
from tests._shared import make_tool_env


def _write_script_skill(skill_dir: pathlib.Path, name: str = "alpha") -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name}
description: test
version: 0.1
type: script
runtime: python3
scripts:
  - name: scripts/run.py
---
""",
        encoding="utf-8",
    )
    (skill_dir / "scripts").mkdir(exist_ok=True)
    (skill_dir / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")


def _write_instruction_skill(
    root: pathlib.Path,
    rel: str,
    *,
    name: str,
    description: str,
    extra: str = "",
    body: str = "",
) -> pathlib.Path:
    skill_dir = root / rel
    skill_dir.mkdir(parents=True, exist_ok=True)
    default_body = f"# {name}\n"
    text = f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body or default_body}"
    path = skill_dir / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    return path


def _discover_openclaw(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: dict | None = None,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    workspace = tmp_path / "workspace"
    drive = tmp_path / "drive"
    home = tmp_path / "home"
    workspace.mkdir()
    drive.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "ouroboros.skills.loader.load_settings",
        lambda: {
            "skills.allowBundled": True,
            "skills.load.extraDirs": [],
            "skills.load.watch": False,
            "skills.load.watchDebounceMs": 250,
            "agents.defaults.skills": None,
            "agents.list": [],
            **(settings or {}),
        },
    )
    return workspace, drive, home


def test_discover_enabled_skill_without_review(tmp_path: pathlib.Path, monkeypatch) -> None:
    del monkeypatch
    repo = tmp_path / "skills" / "external" / "alpha"
    _write_script_skill(repo)
    content_hash = compute_content_hash(repo, manifest_scripts=[{"name": "scripts/run.py"}])
    save_enabled(tmp_path, "alpha", True)
    save_skill_grants(
        tmp_path,
        "alpha",
        [],
        content_hash=content_hash,
        requested_keys=[],
        granted_permissions=[],
        requested_permissions=[],
    )
    skills = discover_skills(tmp_path, repo_path=str(tmp_path / "skills"))
    assert len(skills) == 1
    skill = skills[0]
    assert isinstance(skill, LoadedSkill)
    assert skill.enabled is True
    assert skill.available_for_execution is True
    assert grant_status_for_skill(tmp_path, skill)["all_granted"] is True


def test_compute_content_hash_reuses_unchanged_file_digests(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = tmp_path / "skill"
    _write_script_skill(skill_dir)
    original_open = pathlib.Path.open
    binary_reads: list[pathlib.Path] = []

    def track_binary_reads(path: pathlib.Path, *args, **kwargs):
        if args and args[0] == "rb":
            binary_reads.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", track_binary_reads)

    initial_hash = compute_content_hash(skill_dir)
    initial_reads = len(binary_reads)
    assert initial_reads == 2
    assert compute_content_hash(skill_dir) == initial_hash
    assert len(binary_reads) == initial_reads

    payload = skill_dir / "scripts" / "run.py"
    payload.write_text("print('changed')\n", encoding="utf-8")

    assert compute_content_hash(skill_dir) != initial_hash
    assert len(binary_reads) == initial_reads + 1


def test_discovery_observes_manifest_changes_without_local_invalidation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, drive, _home = _discover_openclaw(tmp_path, monkeypatch)
    manifest = _write_instruction_skill(
        workspace / "skills",
        "first",
        name="first",
        description="first skill",
    )
    initial = find_skill(drive, "first", workspace_root=workspace)
    assert initial is not None

    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("first skill", "updated skill"),
        encoding="utf-8",
    )
    refreshed = find_skill(drive, "first", workspace_root=workspace)

    assert refreshed is not None
    assert refreshed.description == "updated skill"
    assert refreshed.content_hash != initial.content_hash


def test_discover_skills_honors_root_precedence_and_reports_shadowed(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, drive, home = _discover_openclaw(tmp_path, monkeypatch)
    _write_instruction_skill(workspace / "skills", "weather", name="weather", description="workspace winner")
    _write_instruction_skill(
        workspace / ".agents" / "skills", "weather", name="weather", description="workspace agents loser"
    )
    _write_instruction_skill(home / ".agents" / "skills", "weather", name="weather", description="home loser")

    discovered = discover_skills(drive, workspace_root=workspace)
    selected = [skill for skill in discovered if skill.name == "weather" and skill.selected]

    assert len(selected) == 1
    assert selected[0].description == "workspace winner"
    assert selected[0].source_kind == "distribution"
    assert not [skill for skill in discovered if not skill.selected]

    resolved = find_skill(drive, "weather", workspace_root=workspace)
    assert resolved is not None
    assert resolved.skill_path == selected[0].skill_path


def test_distribution_skill_is_enabled_and_granted_without_state_files(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, drive, _home = _discover_openclaw(tmp_path, monkeypatch)
    _write_instruction_skill(
        workspace / "skills",
        "trusted-extension",
        name="trusted-extension",
        description="trusted extension",
        extra=(
            "type: extension\n"
            "entry: plugin.py\n"
            "permissions: [tool, send_message]\n"
            "env_from_settings: [OPENAI_API_KEY]\n"
        ),
    )
    (workspace / "skills" / "trusted-extension" / "plugin.py").write_text(
        "def register(api): pass\n",
        encoding="utf-8",
    )

    loaded = find_skill(drive, "trusted-extension", workspace_root=workspace)

    assert loaded is not None
    assert loaded.distribution_owned is True
    assert loaded.enabled is True
    assert not (drive / "state" / "skills" / "trusted-extension" / "enabled.json").exists()
    assert not (drive / "state" / "skills" / "trusted-extension" / "grants.json").exists()
    grants = grant_status_for_skill(drive, loaded)
    assert grants["all_granted"] is True
    assert grants["granted_permissions"] == ["send_message", "tool"]
    assert grants["granted_keys"] == ["OPENAI_API_KEY"]


def test_discovery_ignores_legacy_data_skill_buckets(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, drive, _home = _discover_openclaw(tmp_path, monkeypatch)
    _write_instruction_skill(workspace / "skills", "repo-skill", name="repo-skill", description="repo")
    for bucket in ("native", "external", "clawhub", "ouroboroshub"):
        _write_instruction_skill(
            drive / "skills" / bucket,
            f"legacy-{bucket}",
            name=f"legacy-{bucket}",
            description="must be ignored",
        )

    discovered = discover_skills(drive, workspace_root=workspace)

    assert [skill.name for skill in discovered] == ["repo-skill"]


def test_skill_status_separates_discovery_from_live_readiness(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, drive, _home = _discover_openclaw(tmp_path, monkeypatch)
    _write_instruction_skill(
        workspace / "skills",
        "not-loaded",
        name="not-loaded",
        description="extension found on disk",
        extra="type: extension\nentry: plugin.py\npermissions: [tool]\n",
    )
    (workspace / "skills" / "not-loaded" / "plugin.py").write_text(
        "def register(api): pass\n",
        encoding="utf-8",
    )

    summary = summarize_skills(drive, workspace_root=workspace)
    row = next(item for item in summary["skills"] if item["name"] == "not-loaded")

    assert row["discovered"] is True
    assert row["live_loaded"] is False
    assert row["ready"] is False
    assert row["blocked_reason"] == "not_live_loaded"
    assert row["tool_surfaces"] == []


def test_discover_skills_supports_nested_grouped_layout(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, drive, _home = _discover_openclaw(tmp_path, monkeypatch)
    path = _write_instruction_skill(
        workspace / "skills",
        "ops/postgres",
        name="postgres-guide",
        description="Use for postgres maintenance",
    )

    discovered = discover_skills(drive, workspace_root=workspace)

    assert [(skill.name, skill.skill_path) for skill in discovered if skill.selected] == [
        ("postgres-guide", path),
    ]


def test_discover_skills_applies_os_bin_env_config_and_allowlist_gates(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, drive, _home = _discover_openclaw(
        tmp_path,
        monkeypatch,
        settings={
            "feature.flags.weather": True,
            "agents.defaults.skills": ["weather"],
        },
    )
    _write_instruction_skill(
        workspace / "skills",
        "weather",
        name="weather",
        description="allowed skill",
        extra=(
            "metadata:\n"
            "  openclaw:\n"
            "    requires:\n"
            "      env: [OPENAI_API_KEY]\n"
            "      config: [feature.flags.weather]\n"
        ),
    )
    _write_instruction_skill(
        workspace / "skills",
        "plan9-only",
        name="plan9-only",
        description="blocked by os",
        extra="metadata:\n  openclaw:\n    os: [plan9]\n",
    )
    _write_instruction_skill(
        workspace / "skills",
        "bin-missing",
        name="bin-missing",
        description="blocked by missing bin",
        extra="metadata:\n  openclaw:\n    requires:\n      bins: [__missing_skill_test_bin__]\n",
    )
    _write_instruction_skill(
        workspace / "skills",
        "not-allowed",
        name="not-allowed",
        description="blocked by allowlist",
    )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from tests.conftest import refresh_test_config

    refresh_test_config()

    report = check_skills(drive, workspace_root=workspace)
    rows = {item["name"]: item for item in report["skills"]}

    assert rows["weather"]["eligible"] is True
    assert rows["weather"]["selected"] is True
    assert rows["plan9-only"]["eligible"] is False
    assert "os" in " ".join(rows["plan9-only"]["blocked_reasons"]).lower()
    assert rows["bin-missing"]["eligible"] is False
    assert "bin" in " ".join(rows["bin-missing"]["blocked_reasons"]).lower()
    assert rows["not-allowed"]["eligible"] is False
    assert "allowlist" in " ".join(rows["not-allowed"]["blocked_reasons"]).lower()


def test_summarize_skills_marks_selected_eligible_entries_for_prompt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, drive, home = _discover_openclaw(tmp_path, monkeypatch)
    winner = _write_instruction_skill(workspace / "skills", "weather", name="weather", description="winner")
    _write_instruction_skill(home / ".agents" / "skills", "weather", name="weather", description="shadowed")
    _write_instruction_skill(
        workspace / "skills",
        "broken",
        name="broken",
        description="",
    )

    summary = summarize_skills(drive, workspace_root=workspace)
    rows = {
        item["name"]: item for item in summary["skills"] if item["location"] == str(winner) or item["name"] == "broken"
    }

    assert rows["weather"]["selected"] is True
    assert rows["weather"]["eligible"] is True
    assert rows["weather"]["location"] == str(winner)
    assert rows["broken"]["eligible"] is False
    assert "description" in " ".join(rows["broken"]["blocked_reasons"]).lower()


def test_check_skills_reports_parse_errors_and_oversized_files(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, drive, _home = _discover_openclaw(tmp_path, monkeypatch)
    broken_dir = workspace / "skills" / "broken"
    broken_dir.mkdir(parents=True, exist_ok=True)
    (broken_dir / "SKILL.md").write_text("---\nname: broken\n", encoding="utf-8")
    huge = _write_instruction_skill(
        workspace / "skills",
        "huge",
        name="huge",
        description="too large",
    )
    huge.write_text(huge.read_text(encoding="utf-8") + ("x" * 300_000), encoding="utf-8")

    report = check_skills(drive, workspace_root=workspace)
    rows = {item["name"]: item for item in report["skills"]}

    assert rows["broken"]["eligible"] is False
    assert "frontmatter" in " ".join(rows["broken"]["blocked_reasons"]).lower()
    assert rows["huge"]["eligible"] is False
    assert "oversized" in " ".join(rows["huge"]["blocked_reasons"]).lower()


def test_manifest_symlink_is_discovered_but_ineligible(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, drive, _home = _discover_openclaw(tmp_path, monkeypatch)
    skill_dir = workspace / "skills" / "manifest-link"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "run.py").write_text(
        '---\nname: manifest-link\ndescription: Manifest symlink fixture\n---\n\nprint("script sentinel")\n',
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").symlink_to("scripts/run.py")

    skill = find_skill(drive, "manifest-link", workspace_root=workspace)

    assert skill is not None
    assert skill.selected is True
    assert skill.eligible is False
    assert "symlink" in " ".join(skill.blocked_reasons).lower()


def test_registry_exposes_list_and_check_skills_but_not_legacy_review_tool(tmp_path: pathlib.Path) -> None:
    catalog, _ctx = make_tool_env(tmp_path / "repo", tmp_path / "drive")
    names = {desc.name for desc in catalog.descriptors() if desc.surface == "core"}

    assert "list_skills" in names
    assert "check_skills" in names
    assert "review_skill" not in names
