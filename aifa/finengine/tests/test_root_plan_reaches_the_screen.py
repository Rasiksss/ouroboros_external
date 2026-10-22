"""План, который предложил автор плана, доходит до экрана событием `root_plan` (ADR-0074)."""

from __future__ import annotations

import json

import pytest

from finengine.assistant.plan_view import root_plan_event
from finengine.assistant.turn_gateway import _Turn


def proposal(**over) -> dict:
    base = {
        "schema_version": "1",
        "base_revision": 0,
        "requirements": [{"id": "R1", "description": "Выручка", "source": "«выручку»"}],
        "assumptions": [{"id": "A1", "description": "Годы отчёта", "reason": "в отчёте два года"}],
        "questions": ["Нужны ли кварталы?"],
        "steps": [
            {
                "id": "S1",
                "objective": "Извлечь выручку",
                "requirement_ids": ["R1"],
                "depends_on": [],
                "inputs": [{"source": "artifact", "ref": "doc", "description": "отчёт"}],
                "execution": {"kind": "scenario", "instruction": "extract_registered_metric"},
                "expected_outputs": ["ExtractedMetric"],
                "acceptance_criteria": [
                    {
                        "id": "C1",
                        "description": "значение со страницей",
                        "verification_method": "чтение артефакта",
                    }
                ],
            }
        ],
    }
    return {**base, **over}


@pytest.mark.parametrize(
    "wrap",
    [
        lambda body: body,
        lambda body: f"```json\n{body}\n```",
        lambda body: f"Теперь у меня есть схема.\n\n```json\n{body}\n```\n",
    ],
    ids=["plain", "fence", "prose-then-fence"],
)
def test_the_planners_final_message_becomes_a_root_plan_event(wrap) -> None:
    event = root_plan_event("t1", wrap(json.dumps(proposal())))

    assert event is not None
    assert (event["type"], event["task_id"]) == ("root_plan", "t1")
    step = event["steps"][0]
    assert (step["id"], step["objective"], step["execution"]) == (
        "S1",
        "Извлечь выручку",
        "scenario: extract_registered_metric",
    )
    assert step["criteria"] == [
        {"id": "C1", "description": "значение со страницей", "verification": "чтение артефакта"}
    ]
    assert event["requirements"] == [{"id": "R1", "description": "Выручка", "source": "«выручку»"}]
    assert event["questions"] == ["Нужны ли кварталы?"]


def test_a_message_that_is_not_a_plan_gives_no_event() -> None:
    assert root_plan_event("t1", "Подзадача выполнена: данные собраны.") is None
    assert root_plan_event("t1", '{"steps": "не список"}') is None
    assert root_plan_event("t1", "```json\n[1, 2]\n```") is None


def test_a_step_without_optional_fields_does_not_break_the_event() -> None:
    event = root_plan_event("t1", json.dumps({"steps": [{"id": "S1", "objective": "Шаг"}]}))

    assert event is not None
    assert event["steps"][0]["criteria"] == [] and event["steps"][0]["depends_on"] == []


def test_the_gateway_sends_the_plan_next_to_the_subtask_result() -> None:
    turn = _Turn.__new__(_Turn)
    turn._subtasks = None
    turn._request = type("Request", (), {"run_id": "r"})()

    fenced = f"```json\n{json.dumps(proposal())}\n```"
    events = turn._subtask_finished("t1", fenced)
    assert [e["type"] for e in events] == ["ouro_service", "root_plan"]
    assert [e["type"] for e in turn._subtask_finished("t2", "Подзадача выполнена.")] == [
        "ouro_service"
    ]
