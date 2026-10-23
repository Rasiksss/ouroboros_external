"""A subtask runs on the model its skill is mapped to; everything else keeps the owner's main model."""

from __future__ import annotations

import pytest

from ouroboros.app.config import skill_run_profile

_MAP = (
    "distribution:aifa-plan-author=cloudru::zai-org/GLM-5.2@high, "
    "distribution:aifa-atom-author=cloudru::deepseek-ai/DeepSeek-V4-Pro"
)
_PLAN_TASK = "Навык: distribution:aifa-plan-author. Составь предложение плана."
_ATOM_TASK = "Навык: distribution:aifa-atom-author.\n\nСоздай атом eph.excel."


@pytest.fixture
def mapped(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL_BY_SKILL", _MAP)


def test_a_mapped_skill_gets_its_model_and_optional_effort(mapped):
    assert skill_run_profile(_PLAN_TASK) == ("cloudru::zai-org/GLM-5.2", "high")
    assert skill_run_profile(_ATOM_TASK) == ("cloudru::deepseek-ai/DeepSeek-V4-Pro", "")


def test_the_bare_skill_name_matches_the_distribution_id(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL_BY_SKILL", "aifa-plan-author=m1")
    assert skill_run_profile(_PLAN_TASK) == ("m1", "")


@pytest.mark.parametrize(
    "description",
    [
        "",
        None,
        "Составь план без навыка.",
        "Навык: distribution:other. Что-то.",
        "Прочти: Навык: distribution:aifa-plan-author.",
    ],
    ids=["empty", "none", "no-prefix", "unmapped-skill", "prefix-not-first"],
)
def test_other_tasks_have_no_profile(mapped, description):
    assert skill_run_profile(description) is None


def test_an_unset_map_changes_nothing(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL_BY_SKILL", "")
    assert skill_run_profile(_PLAN_TASK) is None


def test_malformed_entries_are_skipped_and_the_rest_still_apply(monkeypatch):
    monkeypatch.setenv(
        "OUROBOROS_MODEL_BY_SKILL",
        "no-equals-sign, =no-skill, distribution:aifa-plan-author=, distribution:aifa-atom-author=ok-model@bogus",
    )
    assert skill_run_profile(_PLAN_TASK) is None
    assert skill_run_profile(_ATOM_TASK) == ("ok-model@bogus", "")
