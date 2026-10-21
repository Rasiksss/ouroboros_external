"""Фаза планирования цикла: у `custom_analysis` план составляется отдельным вызовом модели до первого хода
и сразу уходит на экран событием `dynamic_plan`.

Прогон владельца 07.10.2026: план был виден только самому планировщику, а на экране работа проступала
по одному ходу.
"""
from __future__ import annotations

import json

from finengine.contracts import GoalIR
from finengine.skills.spec.dynamic_run import DynamicRun, RunLimits
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.room_files import FileSpec
from finengine.skills.spec.target import Target
from dynamic_support import Script
from test_dynamic_run import _give_up
from test_ephemeral_in_graph import CASE, ENTITY, OWNER

PLAN = ["получить показатели формой роли", "собрать Excel на двух листах"]
BOOK = {"kind": "xlsx", "parts": 2, "formulas": False}


def _plan_reply(plan=PLAN, files=(BOOK,)) -> str:
    return json.dumps({"plan": list(plan), "files": list(files)}, ensure_ascii=False)


def _dynamic(base, replies, plan_replies, *, intent_kind: str = "custom_analysis") -> tuple[DynamicRun, Script, Script, list]:
    planner, drafter = Script(list(replies)), Script(list(plan_replies))
    events: list[dict] = []
    dynamic = DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE,
        goal=GoalIR(raw_query="извлеки показатели и собери Excel на двух листах", intent_kind=intent_kind),
        target=Target(output_type="NarrativeOutput", entity_ref=ENTITY), inputs=RunInputs(),
        complete_fn=planner, plan_fn=drafter, event_sink=events.append, limits=RunLimits(max_moves=3),
    )
    return dynamic, planner, drafter, events


def test_the_plan_is_drafted_from_the_full_state_and_shown_before_the_first_move(base) -> None:
    dynamic, planner, drafter, events = _dynamic(base, [_give_up()], [_plan_reply()])

    dynamic.run(deterministic_error="вид цели без корня")

    assert events[0] == {"type": "dynamic_plan", "plan": PLAN, "files": ["xlsx (листов или слайдов не меньше 2)"]}
    assert [event["type"] for event in events[1:]] == ["dynamic_move"]
    assert "## Вопрос пользователя" in drafter.prompts[0] and "## Каталог атомов" in drafter.prompts[0]
    assert "1. получить показатели формой роли" in planner.prompts[0]
    assert dynamic.expected_files == (FileSpec(kind="xlsx", parts=2, formulas=False),)


def test_other_goals_enter_the_cycle_without_a_plan_phase(base) -> None:
    dynamic, planner, drafter, events = _dynamic(base, [_give_up()], [_plan_reply()], intent_kind="document_qa")

    dynamic.run(deterministic_error="нет пути")

    assert drafter.prompts == []
    assert "плана нет" in planner.prompts[0]
    assert [event["type"] for event in events] == ["dynamic_move"]


def test_a_failed_plan_phase_does_not_stop_the_cycle(base) -> None:
    dynamic, planner, _, events = _dynamic(base, [_give_up()], ["не json"])

    result = dynamic.run(deterministic_error="вид цели без корня")

    assert result.status == "gave_up"
    assert "плана нет" in planner.prompts[0]
    assert [event["type"] for event in events] == ["dynamic_move"]


def test_a_plan_rewritten_by_a_move_is_shown_again(base) -> None:
    rewritten = json.loads(_give_up())
    rewritten["plan"] = ["другой путь"]
    dynamic, _, _, events = _dynamic(base, [json.dumps(rewritten, ensure_ascii=False)], [_plan_reply(files=())])

    dynamic.run(deterministic_error="вид цели без корня")

    plans = [event for event in events if event["type"] == "dynamic_plan"]
    assert [event["plan"] for event in plans] == [PLAN, ["другой путь"]]
