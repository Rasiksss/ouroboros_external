"""Остановка пользователем внутри цикла (Р14.3, ревью части 14) и карточка входа в цикл (Р14.4).

Рабочий поток доживает до конца сам (``live_stream``), поэтому цикл сам спрашивает ``is_cancelled`` перед каждым ходом
и перед каждым сценарием хода: ход в полёте дорабатывает, следующий не начинается.
"""
# ruff: noqa: F811 — фикстуры подключены общим conftest
from __future__ import annotations

import asyncio

import pytest

from test_dynamic_run import DynamicScenario, _give_up, _move, _run


class StopAfterScenario:
    """Сценарий-подставка: после первого вызова «пользователь уходит»."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0
        self.left = False

    def __call__(self, text: str):
        self.calls += 1
        self.left = True
        return self.inner(text)


def test_a_cycle_does_not_start_the_next_move_after_the_user_left(base) -> None:
    scenario = StopAfterScenario(DynamicScenario(base))
    dynamic, planner = _run(base, [_move(scenarios=("Извлеки",)), _move(scenarios=("Ещё",)), _give_up()], scenario=scenario)
    dynamic.is_cancelled = lambda: scenario.left

    with pytest.raises(asyncio.CancelledError):
        dynamic.run(deterministic_error="нет пути")

    assert scenario.calls == 1 and len(planner.prompts) == 1, "ход в полёте дорабатывает, второй не начат"


def test_a_move_does_not_start_the_next_scenario_after_the_user_left(base) -> None:
    scenario = StopAfterScenario(DynamicScenario(base))
    dynamic, planner = _run(base, [_move(scenarios=("Первый", "Второй")), _give_up()], scenario=scenario)
    dynamic.is_cancelled = lambda: scenario.left

    with pytest.raises(asyncio.CancelledError):
        dynamic.run(deterministic_error="нет пути")

    assert scenario.calls == 1, "второй сценарий хода не запущен"


def test_a_cycle_whose_user_stayed_runs_to_the_end(base) -> None:
    dynamic, planner = _run(base, [_give_up()], scenario=DynamicScenario(base))
    dynamic.is_cancelled = lambda: False

    assert dynamic.run(deterministic_error="нет пути").status == "gave_up"


# ---- карточка входа в цикл после падения графа (Р14.4) ---------------------------------------------

def _events_of(base, stage: str) -> list[dict]:
    events: list[dict] = []
    dynamic, _planner = _run(base, [_give_up()], scenario=DynamicScenario(base))
    dynamic.event_sink = events.append
    dynamic.entry_stage = stage
    dynamic.run(deterministic_error="ход завершён с ошибками\n✗ read.value → нет результата (0): сбой")
    return [event for event in events if event["type"] == "dynamic_move"]


def test_the_cycle_that_took_over_a_failed_graph_opens_the_trace_with_the_entry_card(base) -> None:
    moves = _events_of(base, "execution")

    assert moves[0]["move"] == 0 and moves[0]["action"] == "entry" and moves[0]["status"] == "done"
    assert moves[0]["reason"].startswith("✗ read.value"), "причина — первый упавший узел"
    assert "✗ read.value" in moves[0]["details"], "причина входа — итог узлов первой попытки"
    assert [move["move"] for move in moves[1:]] == [1], "ходы цикла нумеруются с единицы, как раньше"


def test_a_planning_failure_has_no_entry_card(base) -> None:
    assert [move["action"] for move in _events_of(base, "planning")] == ["give_up"]
