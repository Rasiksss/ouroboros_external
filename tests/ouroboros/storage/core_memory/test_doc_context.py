from tests.helpers.execution_memory import sql_execution_memory_repository

"""Tests for documentation context loading invariants."""

import pathlib
import tempfile

import pytest

from ouroboros.storage.core_memory.scratchpad import get_scratchpad
from tests.helpers.shared import TEST_SCOPE, make_skill_surface


@pytest.fixture(autouse=True)
def _reset_core_memory_services() -> None:
    """Drop the cached service so this test binds to its own runtime DB.

    Context assembly reads the scratchpad, so these tests reach the cached
    factory even though they never name it.
    """
    get_scratchpad.cache_clear()


def _make_env(tmpdir: pathlib.Path):
    from ouroboros.agent import Env

    repo_dir = tmpdir / "repo"
    drive_root = tmpdir / "drive"
    repo_dir.mkdir(parents=True, exist_ok=True)
    drive_root.mkdir(parents=True, exist_ok=True)
    for subdir in ["state", "memory", "memory/knowledge", "logs"]:
        (drive_root / subdir).mkdir(parents=True, exist_ok=True)
    (repo_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (repo_dir / "docs").mkdir(parents=True, exist_ok=True)
    (repo_dir / "prompts" / "SYSTEM.md").write_text("You are Ouroboros.", encoding="utf-8")
    (repo_dir / "docs" / "ARCHITECTURE.md").write_text("# Ouroboros v5.5.0 — Architecture", encoding="utf-8")
    (repo_dir / "docs" / "DEVELOPMENT.md").write_text("# DEVELOPMENT.md — Dev Guide", encoding="utf-8")
    (repo_dir / "README.md").write_text(
        "[![Version 5.5.0](https://img.shields.io/badge/version-5.5.0-green.svg)](VERSION)", encoding="utf-8"
    )
    (repo_dir / "docs" / "CHECKLISTS.md").write_text("## Repo Commit Checklist\n| # | item |", encoding="utf-8")
    from tests.helpers.owner_state import seed_owner_state

    seed_owner_state(drive_root, spent_calls=0)
    (drive_root / "memory" / "scratchpad.md").write_text("test scratchpad", encoding="utf-8")
    return Env(repo_dir=repo_dir, drive_root=drive_root)


def _build_system_text(task_overrides=None):
    from ouroboros.agent.context import build_llm_messages

    tmpdir = pathlib.Path(tempfile.mkdtemp())
    env = _make_env(tmpdir)
    task = {"id": "test-1", "type": "task", "text": "hello"}
    if task_overrides:
        task.update(task_overrides)
    messages, _ = build_llm_messages(
        env=env,
        task=task,
        scope=TEST_SCOPE,
        skill_surface=make_skill_surface(repo_dir=env.repo_dir, drive_root=env.drive_root),
        execution_memory_repository=sql_execution_memory_repository(),
    )
    content = messages[0]["content"]
    return " ".join(block.get("text", "") for block in content if isinstance(block, dict))


def test_regular_tasks_exclude_development_and_governance_docs():
    text = _build_system_text({"type": "task"})
    for forbidden in [
        "ARCHITECTURE.md",
        "Ouroboros v5.5.0",
        "DEVELOPMENT.md",
        "Dev Guide",
        "README.md",
        "CHECKLISTS.md",
        "Repo Commit Checklist",
    ]:
        assert forbidden not in text
