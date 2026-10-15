"""Часть 9: полный ход чата сохраняет общий с frontend поток событий."""
from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from finengine.contracts import ScalarFact
from finengine.skills.spec.ephemeral import DOWNLOAD_SCHEMA
from tests.integration.conftest import _make_fake_locate_and_read_value_executors
from tests.integration.ingress_support import (  # noqa: F401
    Turn,
    gate_env,
    patched_build_registry,
    set_intent,
)

from dynamic_support import assert_event_fixture, normalized_events
from test_download_atom import _serve


PlannerReply = dict | str | Callable[[str], dict | str]


class SequenceModel:
    def __init__(self, replies: list[PlannerReply]) -> None:
        self.replies = replies
        self.prompts: list[str] = []

    async def __call__(self, system: str, prompt: str) -> str:  # noqa: ARG002
        self.prompts.append(prompt)
        reply = self.replies[min(len(self.prompts), len(self.replies)) - 1]
        if callable(reply):
            reply = reply(prompt)
        return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)


def _plan(*, intent_kind: str = "custom_analysis", unresolved=()) -> dict:
    return {
        "route": "plan", "intent_kind": intent_kind, "targets": [],
        "entity_ref": None, "period_ref": None, "role": None,
        "unresolved": list(unresolved), "ambiguities": [],
    }


def _execute(*steps: dict, new_atoms=()) -> dict:
    return {
        "objective": "получить данные", "reason": "они нужны для ответа", "action": "execute",
        "scenarios": [], "new_atoms": list(new_atoms), "steps": list(steps),
        "answer": "", "evidence_ids": [],
    }


def _give_up(answer: str) -> dict:
    return {
        "objective": "остановиться", "reason": answer, "action": "give_up",
        "scenarios": [], "new_atoms": [], "steps": [], "answer": answer, "evidence_ids": [],
    }


def _two_period_read_value(items, ctx):  # noqa: ARG001
    values = (("6M 2025", 1_398_538.772), ("6M 2026", 1_453_957.053))
    return {
        args.concept: tuple(
            ScalarFact(
                produced_by_atom="read.value", produced_by_atom_version="1.0.0",
                source_ref=f"fake:{args.concept}:{period}", concept=args.concept,
                period_ref=period, value=value, unit="RUB_MLN",
            )
            for period, value in values if args.concept == "SALES_REVENUE"
        )
        for args in items
    }


def _finalize_cv(prompt: str) -> dict:
    metric_ids = [
        line.split("|", 1)[0].strip()
        for line in prompt.splitlines()
        if "| ExtractedMetric |" in line
    ]
    dynamic_id = next(
        line.split("|", 1)[0].strip()
        for line in prompt.splitlines()
        if "| dyn:cv_sales_revenue |" in line
    )
    return {
        "objective": "ответить", "reason": "расчёт готов", "action": "finalize",
        "scenarios": [], "new_atoms": [], "steps": [],
        "answer": (
            "Исходные значения: 6M 2025 — 1 398 538,772 млн руб.; "
            "6M 2026 — 1 453 957,053 млн руб.\n"
            "Формула: CV = population_sd / mean × 100. CV ≈ 1,9428 %."
        ),
        "evidence_ids": [*list(dict.fromkeys(metric_ids))[-2:], dynamic_id],
    }


def _finalize_revenue(prompt: str) -> dict:
    ids = [
        line.split("|", 1)[0].strip()
        for line in prompt.splitlines()
        if "| ExtractedMetric |" in line
    ]
    return {
        "objective": "ответить", "reason": "доступная часть найдена", "action": "finalize",
        "scenarios": [], "new_atoms": [], "steps": [],
        "answer": "Выручка найдена за два периода. EBITDA в отчёте не найдена.",
        "evidence_ids": list(dict.fromkeys(ids))[-2:],
    }


def _arm_dynamic(monkeypatch, replies: list[PlannerReply]) -> SequenceModel:
    model = SequenceModel(replies)
    monkeypatch.setattr("finengine.skills.spec.dynamic_run._default_complete", model)
    return model


def _run_turn(monkeypatch, message: str) -> tuple[Turn, list[dict]]:
    turn = Turn()
    turn.arm(monkeypatch)
    return turn, turn.run(message)


def test_finalized_with_cards_and_text_matches_shared_fixture(
    monkeypatch, local_room,
) -> None:
    set_intent(monkeypatch, _plan())
    locate, _ = _make_fake_locate_and_read_value_executors({"SALES_REVENUE": 1.0})
    patched_build_registry(monkeypatch, locate, _two_period_read_value)
    atom = {
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
    planner = _arm_dynamic(monkeypatch, [
        _execute({"atom": "read.value", "concept": "SALES_REVENUE"}),
        _execute({"atom": atom["name"]}, new_atoms=(atom,)),
        _finalize_cv,
    ])
    author = SequenceModel([{
        "code": (
            "def run(inputs):\n"
            "    xs = [m['scalar_fact']['value'] for m in inputs['ExtractedMetric']]\n"
            "    mean = sum(xs) / len(xs)\n"
            "    sd = (sum((x - mean) ** 2 for x in xs) / len(xs)) ** 0.5\n"
            "    return {'mean': mean, 'population_sd': sd, 'cv_pct': sd / mean * 100}\n"
        ),
        "example_inputs": {},
    }])
    monkeypatch.setattr("finengine.skills.spec.atom_author._default_complete", author)

    turn, events = _run_turn(monkeypatch, "Рассчитай CV выручки по двум периодам")

    assert turn.dispatcher_calls == 0
    assert len([event for event in events if event.get("type") == "text_delta"]) == 1
    typed = [event["result"] for event in events if event.get("type") == "typed_result"]
    assert {(item["concept"], item["period_ref"]) for item in typed} == {
        ("SALES_REVENUE", "6M 2025"), ("SALES_REVENUE", "6M 2026"),
    }
    assert "| dyn:cv_sales_revenue |" in planner.prompts[-1]
    assert not any(event.get("type") == "typed_error" for event in events)
    assert_event_fixture("finalized-with-cards-and-text.json", normalized_events(events))


def test_gave_up_is_one_answer_without_graph_failure(monkeypatch) -> None:
    set_intent(monkeypatch, _plan())
    atom = {
        "name": "eph.external_csv", "purpose": "недоступный внешний CSV",
        "requires": [], "output_type": "dyn:external_csv",
        "output_schema": DOWNLOAD_SCHEMA, "url": "https://data.example.org/unavailable.csv",
    }
    _serve(monkeypatch, lambda request: httpx.Response(503, text="upstream unavailable"))
    _arm_dynamic(monkeypatch, [
        _execute({"atom": atom["name"]}, new_atoms=(atom,)),
        _give_up("Внешний источник сейчас недоступен, поэтому получить данные не удалось."),
    ])

    turn, events = _run_turn(monkeypatch, "Проверь внешний CSV")

    assert turn.dispatcher_calls == 0
    assert [event["text"] for event in events if event.get("type") == "text_delta"] == [
        "Внешний источник сейчас недоступен, поэтому получить данные не удалось."
    ]
    assert not any(event.get("type") == "typed_error" for event in events)
    assert not any(
        event.get("type") == "typed_error" and event.get("error", {}).get("code") == "graph_execution_failed"
        for event in events
    )
    assert_event_fixture("gave-up-as-answer.json", normalized_events(events))


def test_stopped_with_partials_is_one_answer(monkeypatch) -> None:
    set_intent(monkeypatch, _plan())
    locate, _ = _make_fake_locate_and_read_value_executors({"SALES_REVENUE": 1.0})
    patched_build_registry(monkeypatch, locate, _two_period_read_value)
    _arm_dynamic(monkeypatch, [
        _execute({"atom": "read.value", "concept": "SALES_REVENUE"}),
        "не json", "не json", "не json", "не json",
    ])

    turn, events = _run_turn(monkeypatch, "Найди выручку и EBITDA")

    assert turn.dispatcher_calls == 0
    assert len([event for event in events if event.get("type") == "text_delta"]) == 1
    typed = [event["result"] for event in events if event.get("type") == "typed_result"]
    assert {(item["concept"], item["period_ref"]) for item in typed} == {
        ("SALES_REVENUE", "6M 2025"), ("SALES_REVENUE", "6M 2026"),
    }
    assert not any(event.get("type") == "typed_error" for event in events)
    assert_event_fixture("stopped-with-partials.json", normalized_events(events))


def test_partial_finalize_with_revenue_and_missing_ebitda_matches_shared_fixture(
    monkeypatch,
) -> None:
    set_intent(monkeypatch, _plan())
    locate, _ = _make_fake_locate_and_read_value_executors({"SALES_REVENUE": 1.0})
    patched_build_registry(monkeypatch, locate, _two_period_read_value)
    _arm_dynamic(monkeypatch, [
        _execute({"atom": "read.value", "concept": "SALES_REVENUE"}),
        _finalize_revenue,
    ])

    _, events = _run_turn(monkeypatch, "Найди выручку и EBITDA")

    assert len([event for event in events if event.get("type") == "text_delta"]) == 1
    typed = [event["result"] for event in events if event.get("type") == "typed_result"]
    assert {(item["concept"], item["period_ref"]) for item in typed} == {
        ("SALES_REVENUE", "6M 2025"), ("SALES_REVENUE", "6M 2026"),
    }
    assert_event_fixture("finalized-partial-answer.json", normalized_events(events))


def test_deterministic_remainder_with_cards_matches_shared_fixture(
    monkeypatch,
) -> None:
    set_intent(monkeypatch, {
        **_plan(intent_kind="extract_registered_metric", unresolved=("сделай презентацию",)),
        "targets": [{"concept": "SALES_REVENUE"}],
    })
    locate, read = _make_fake_locate_and_read_value_executors({"SALES_REVENUE": 42085.931000000004})
    patched_build_registry(monkeypatch, locate, read)

    _, events = _run_turn(monkeypatch, "Покажи выручку и сделай презентацию")

    assert len([event for event in events if event.get("type") == "typed_result"]) == 1
    text = next(event["text"] for event in events if event.get("type") == "text_delta")
    assert "Не выполнено: сделай презентацию" in text
    assert_event_fixture("deterministic-not-performed.json", normalized_events(events))
