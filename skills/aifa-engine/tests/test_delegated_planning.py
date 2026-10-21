"""Delegated proposals and root acceptance stay outside AIFA."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_root_delegates_and_accepts_full_proposals():
    guide = (ROOT / "skills/aifa-planning/SKILL.md").read_text(encoding="utf-8")
    detail = (ROOT / "skills/aifa-planning/references/plans.md").read_text(encoding="utf-8")
    assert "distribution:aifa-plan-author" in guide
    assert 'task_plan(action="accept"' in detail
    assert "awaiting_review" in detail and "observation_refs" in detail
    assert "aifa_report" in detail and "500" in detail


def test_planner_returns_json_and_does_not_finish_with_a_short_report():
    guide = (ROOT / "skills/aifa-plan-author/SKILL.md").read_text(encoding="utf-8")
    assert 'task_plan(action="schema")' in guide
    assert "финальным сообщением" in guide and "JSON" in guide
    assert "aifa_report" in guide and "не вызывай" in guide
    assert "depends_on" in guide and "verification_method" in guide


def test_planner_skill_is_a_discoverable_instruction():
    from new_core.skills.manifest import parse_skill_manifest_text

    manifest = parse_skill_manifest_text((ROOT / "skills/aifa-plan-author/SKILL.md").read_text(encoding="utf-8"))
    assert manifest.validate() == []
    assert manifest.type == "instruction" and manifest.name == "aifa-plan-author"
    assert len(manifest.description) <= 260
