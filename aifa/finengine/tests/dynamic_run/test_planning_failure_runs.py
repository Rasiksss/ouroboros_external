"""Часть 13 доводки: полные прогоны. Исходная фраза приёмки 06.10.2026 — до пользовательского исхода.

Вход — настоящий ``FinengineAgent.query_stream`` → ``execute_routed_goal``; ``Case`` — SQLite; код
временного атома исполняет ``local_room``. Подставные только модели: классификатор и планировщик с
автором атомов. Живой разбор фразы (П13.4) — в конце файла, без ``AIFA_LIVE_MODEL=1`` пропускается.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from finengine.skills import production_runtime as production
from tests.integration.conftest import _make_fake_locate_and_read_value_executors
from tests.integration.ingress_support import (  # noqa: F401
    Turn,
    gate_env,
    patched_build_registry,
    set_intent,
    set_intent_sequence,
)

from dynamic_support import assert_event_fixture, normalized_events
from test_chat_outcome_contract import (
    SequenceModel,
    _arm_dynamic,
    _execute,
    _finalize_cv,
    _two_period_read_value,
)

MESSAGE = "извлеки выручку и посчитай для неё коэффициент вариации"
ANALYSIS_PART = "посчитай для неё коэффициент вариации"
_PLAN = {"route": "plan", "targets": [], "entity_ref": None, "period_ref": None, "role": None, "ambiguities": []}

COMPOUND_FORM = {
    **_PLAN, "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
    "unresolved": [], "goal_ref": "goal-1", "depends_on": [],
    "additional_goals": [{
        **_PLAN, "goal_ref": "goal-2", "intent_kind": "custom_analysis", "depends_on": ["goal-1"],
        "unresolved": [ANALYSIS_PART],
    }],
}
SINGLE_FORM = {**_PLAN, "intent_kind": "custom_analysis", "unresolved": [MESSAGE]}
SCENARIO_PARSE = {
    **_PLAN, "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
    "unresolved": [],
}
CV_ATOM = {
    "name": "eph.calc_cv_sales_revenue", "purpose": "коэффициент вариации выручки",
    "requires": [{"input_type": "ExtractedMetric", "concept": "SALES_REVENUE"}],
    "output_type": "dyn:cv_sales_revenue",
    "output_schema": {
        "type": "object", "required": ["mean", "population_sd", "cv_pct"],
        "properties": {
            "mean": {"type": "number", "x-unit": "RUB_MLN"},
            "population_sd": {"type": "number", "x-unit": "RUB_MLN"},
            "cv_pct": {"type": "number", "x-unit": "percent"},
        },
    },
}
CV_CODE = (
    "def run(inputs):\n"
    "    xs = [m['scalar_fact']['value'] for m in inputs['ExtractedMetric']]\n"
    "    mean = sum(xs) / len(xs)\n"
    "    sd = (sum((x - mean) ** 2 for x in xs) / len(xs)) ** 0.5\n"
    "    return {'mean': mean, 'population_sd': sd, 'cv_pct': sd / mean * 100}\n"
)


def _arm_models(monkeypatch, replies: list) -> SequenceModel:
    locate, _ = _make_fake_locate_and_read_value_executors({"SALES_REVENUE": 1.0})
    patched_build_registry(monkeypatch, locate, _two_period_read_value)
    monkeypatch.setattr(
        "finengine.skills.spec.atom_author._default_complete",
        SequenceModel([{"code": CV_CODE, "example_inputs": {}}]),
    )
    return _arm_dynamic(monkeypatch, replies)


def _count_cycles(monkeypatch) -> list:
    created: list = []
    real = production.DynamicRun

    def _make(**kwargs):
        created.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(production, "DynamicRun", _make)
    return created


def _turn(monkeypatch, message: str = MESSAGE) -> tuple[Turn, list[dict]]:
    turn = Turn()
    turn.arm(monkeypatch)
    return turn, turn.run(message)


@pytest.mark.parametrize("form", [COMPOUND_FORM, SINGLE_FORM], ids=["P13.1-compound-form", "P13.2-single-custom-analysis"])
def test_P13_1_P13_2_the_original_phrase_is_one_cycle_scenario_then_temporary_atom_then_cv(
    monkeypatch, local_room, form,
) -> None:
    classifier = set_intent_sequence(monkeypatch, [form, SCENARIO_PARSE])
    planner = _arm_models(monkeypatch, [
        {**_execute(), "scenarios": ["Извлеки выручку за все периоды"]},
        _execute({"atom": CV_ATOM["name"]}, new_atoms=(CV_ATOM,)),
        _finalize_cv,
    ])
    cycles = _count_cycles(monkeypatch)

    turn, events = _turn(monkeypatch)

    errors = [event for event in events if event.get("type") == "typed_error"]
    assert not errors, errors
    assert turn.dispatcher_calls == 0 and len(cycles) == 1, "один цикл на сообщение, вложенного нет"
    assert len(classifier.calls) == 2, "разбор сообщения и разбор сценария планировщика — не третий"
    text = next(event["text"] for event in events if event.get("type") == "text_delta")
    assert "1,9428" in text and "CV" in text, text
    typed = [event["result"] for event in events if event.get("type") == "typed_result"]
    assert {(item["concept"], item["period_ref"]) for item in typed} == {
        ("SALES_REVENUE", "6M 2025"), ("SALES_REVENUE", "6M 2026"),
    }
    # К13.4: evidence сценария видны следующему ходу, атом читает их, а не документ
    assert "| ExtractedMetric | SALES_REVENUE |" in planner.prompts[1]
    assert "| dyn:cv_sales_revenue |" in planner.prompts[2]
    if form is COMPOUND_FORM:
        # К13.2/К13.3 на настоящем входе: планировщик видел обе части и зависимость
        first = planner.prompts[0]
        assert "goal-1" in first and "goal-2" in first and "depends_on=['goal-1']" in first and ANALYSIS_PART in first


def test_K13_4_a_scenario_that_itself_needs_the_cycle_is_refused_to_the_planner_not_nested(
    monkeypatch, local_room,
) -> None:
    """Сценарий планировщика разобран как составная цель с динамической частью (путь 1 внутри цикла)."""
    classifier = set_intent_sequence(monkeypatch, [COMPOUND_FORM, COMPOUND_FORM])
    planner = _arm_models(monkeypatch, [
        {**_execute(), "scenarios": ["Извлеки выручку и посчитай коэффициент вариации"]},
        {
            "objective": "остановиться", "reason": "сценарий отказал", "action": "give_up", "scenarios": [],
            "new_atoms": [], "steps": [], "answer": "Расчёт не удался.", "evidence_ids": [],
        },
    ])
    cycles = _count_cycles(monkeypatch)

    _, events = _turn(monkeypatch)

    assert len(cycles) == 1 and len(classifier.calls) == 2
    assert "scenario_not_deterministic" in planner.prompts[1], "планировщик получил отказ сценария"
    assert not [event for event in events if event.get("type") == "typed_error"]


def test_K13_5_a_compound_goal_answers_in_the_same_form_as_a_single_custom_analysis(monkeypatch, local_room) -> None:
    """Тот же поток, что у одиночной custom_analysis: файл читает и frontend-тест раскладки ответа."""
    set_intent(monkeypatch, COMPOUND_FORM)
    _arm_models(monkeypatch, [
        _execute({"atom": "read.value", "concept": "SALES_REVENUE"}),
        _execute({"atom": CV_ATOM["name"]}, new_atoms=(CV_ATOM,)),
        _finalize_cv,
    ])

    turn, events = _turn(monkeypatch, "Рассчитай CV выручки по двум периодам")

    assert turn.dispatcher_calls == 0
    assert_event_fixture("finalized-with-cards-and-text.json", normalized_events(events))


def test_P13_3_two_deterministic_parts_are_planned_together_without_a_cycle(monkeypatch) -> None:
    set_intent(monkeypatch, {
        **_PLAN, "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
        "unresolved": [], "goal_ref": "goal-1", "depends_on": [],
        "additional_goals": [{
            **_PLAN, "goal_ref": "goal-2", "intent_kind": "extract_registered_metric",
            "targets": [{"concept": "NET_INCOME"}], "depends_on": [], "unresolved": [],
        }],
    })
    locate, read = _make_fake_locate_and_read_value_executors({"SALES_REVENUE": 100.0, "NET_INCOME": 25.0})
    patched_build_registry(monkeypatch, locate, read)
    planner_calls: list = []

    async def _no_planner(system: str, prompt: str) -> str:
        planner_calls.append(prompt)
        raise AssertionError("детерминированная составная цель не должна запускать цикл")

    monkeypatch.setattr("finengine.skills.spec.dynamic_run._default_complete", _no_planner)
    cycles = _count_cycles(monkeypatch)

    _, events = _turn(monkeypatch, "извлеки выручку и чистую прибыль")

    errors = [event for event in events if event.get("type") == "typed_error"]
    assert not errors, errors
    assert not cycles and not planner_calls
    assert {event["result"]["concept"] for event in events if event.get("type") == "typed_result"} == {
        "SALES_REVENUE", "NET_INCOME",
    }
    assert any("compound" in event.get("planner_version", "") for event in events if event.get("type") == "plan_graph")


@pytest.mark.skipif(os.getenv("AIFA_LIVE_MODEL") != "1", reason="живой разбор: нужен AIFA_LIVE_MODEL=1 и ключ модели")
def test_P13_4_live_classifier_form_for_the_original_phrase_enters_one_cycle(monkeypatch) -> None:
    """Какую форму возвращает живая модель; допустимы обе. Результат — в журнале, не критерий формы."""
    from finengine.skills.atoms.intent import IngressContext, parse_message

    decision = asyncio.run(parse_message(MESSAGE, context=IngressContext(ui_role=""), allow_answer=False))
    goal = decision.goal
    kinds = [clause.intent_kind for clause in goal.clauses()]
    print("P13.4 live form:", json.dumps(
        [(c.goal_ref, c.intent_kind, c.depends_on) for c in goal.clauses()], ensure_ascii=False,
    ))

    assert kinds in (["custom_analysis"], ["extract_registered_metric", "custom_analysis"]), kinds
    if len(kinds) == 2:
        assert goal.clauses()[1].depends_on == [goal.clauses()[0].goal_ref]
