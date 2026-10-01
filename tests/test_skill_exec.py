"""Skill exec tools without review_skill."""

from __future__ import annotations

import json
import pathlib

import pytest

from new_core.tools.context import ToolContext
from ouroboros.tools import skill_exec as skill_exec_mod


@pytest.fixture
def ctx(tmp_path: pathlib.Path, monkeypatch) -> ToolContext:
    del monkeypatch
    repo = tmp_path / "repo"
    skills_root = repo / "skills" / "alpha"
    skills_root.mkdir(parents=True)
    (skills_root / "SKILL.md").write_text(
        """---
name: alpha
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
    scripts = skills_root / "scripts"
    scripts.mkdir()
    (scripts / "run.py").write_text("print('hi')\n", encoding="utf-8")
    return ToolContext(repo_dir=repo, drive_root=tmp_path)


def test_distribution_skill_exec_does_not_require_manual_enablement(ctx: ToolContext) -> None:
    result = json.loads(skill_exec_mod._handle_skill_exec(ctx, skill="alpha", script="scripts/run.py"))
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hi"


def test_skill_exec_lifecycle_event_reaches_the_queue(ctx: ToolContext, tmp_path: pathlib.Path) -> None:
    """The payload the supervisor's skill_lifecycle handler routes on is unchanged.

    The tool now names the event and what happened; type, timestamp, task id and delivery
    belong to the events channel.
    """
    import queue as queue_mod

    from ouroboros.tools.events import QueueToolEvents

    q: queue_mod.Queue = queue_mod.Queue()
    ctx.events = QueueToolEvents(q, tmp_path, task_id="t-skill")

    result = json.loads(skill_exec_mod._handle_skill_exec(ctx, skill="alpha", script="scripts/run.py"))
    assert result["exit_code"] == 0

    event = q.get_nowait()
    assert event["type"] == "skill_exec_finished"
    assert event["task_id"] == "t-skill"
    assert event["skill"] == "alpha"
    assert event["script"] == "scripts/run.py"
    assert event["exit_code"] == 0
    assert event["ts"]


def test_skill_exec_lifecycle_falls_back_to_the_event_stream(
    ctx: ToolContext, tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Without a queue the event still has to land: recorded, and republished locally."""
    from ouroboros.tools.events import QueueToolEvents

    recorded: list[tuple[str, dict]] = []
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ouroboros.tools.events.record_event",
        lambda root, stream, event_type, payload, **kw: recorded.append((event_type, payload)),
    )
    monkeypatch.setattr(
        "ouroboros.observability.event_bus.publish_event",
        lambda topic, data: published.append((topic, data)),
    )
    ctx.events = QueueToolEvents(None, tmp_path, task_id="t-skill")

    json.loads(skill_exec_mod._handle_skill_exec(ctx, skill="alpha", script="scripts/run.py"))

    assert [name for name, _ in recorded] == ["skill_exec_finished"]
    assert recorded[0][1]["skill"] == "alpha"
    assert [topic for topic, _ in published] == ["skill.lifecycle"]
