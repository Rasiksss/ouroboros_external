"""Предохранитель «Не выполнено»: действенный остаток `unresolved` не теряется молча."""
from __future__ import annotations

from finengine.contracts import GoalIR
from finengine.skills.production_runtime import unperformed_requests

ACTION = "проведи корреляционный анализ, результат анализа верни в excel"


def _goal(kind: str = "extract_registered_metric", unresolved=(), **extra) -> GoalIR:
    return GoalIR(raw_query="q", intent_kind=kind, unresolved=list(unresolved), **extra)


def test_an_action_phrase_left_in_unresolved_is_reported_verbatim() -> None:
    assert unperformed_requests(_goal(unresolved=[ACTION])) == [ACTION]


def test_nothing_is_reported_when_unresolved_is_empty() -> None:
    assert unperformed_requests(_goal()) == []


def test_the_metric_phrase_of_an_ad_hoc_goal_is_not_a_lost_request() -> None:
    for kind in ("extract_ad_hoc_metric", "calculate_ad_hoc_metric"):
        assert unperformed_requests(_goal(kind, unresolved=["средняя ставка по кредитам"])) == []


def test_an_action_after_the_ad_hoc_metric_phrase_is_still_reported() -> None:
    goal = _goal("extract_ad_hoc_metric", unresolved=["средняя ставка по кредитам", ACTION])
    assert unperformed_requests(goal) == [ACTION]


def test_an_invalid_role_is_not_reported_here() -> None:
    assert unperformed_requests(_goal(unresolved=["role:astronaut"])) == []


def test_a_concept_code_outside_the_candidates_is_reported_as_an_unrecognized_metric() -> None:
    (line,) = unperformed_requests(_goal(unresolved=["NET_INCOME_OPERATING_PROFAT"]))
    assert "показатель не распознан" in line and "NET_INCOME_OPERATING_PROFAT" in line


def test_every_clause_of_a_compound_goal_is_checked() -> None:
    goal = _goal(
        unresolved=[ACTION],
        additional_goals=[{
            "goal_ref": "goal-2", "raw_query": "q", "intent_kind": "extract_ad_hoc_metric",
            "unresolved": ["фраза показателя", "сделай презентацию"],
        }],
    )
    assert unperformed_requests(goal) == [ACTION, "сделай презентацию"]
