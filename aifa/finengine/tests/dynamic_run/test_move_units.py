"""Все независимые действия одного хода идут одновременно, сбой действия — трейс корню, а не падение хода (ADR-0092).

Прогон 27.10 (ed53d54a): `web.search` из `steps` стартовал только после трёх частей цели, потому что `_move` шёл
фазами «части → шаги». Ход состоит из независимых действий (части цели, шаги атомов); зависимое корень переносит в
следующий ход. Исключение в любом действии становится отказом этого действия с хвостом трейса; остальные
действия доходят до конца, `aifa_move` отвечает текстом, а не 500.
"""
from __future__ import annotations

import threading

import pytest

from finengine.llm.budget import BudgetExceeded
from finengine.skills.spec.dynamic_run import GOAL_OBTAINED, Decision, MoveOutcome
from finengine.store.cases import CaseConflictError
from test_parallel_goals import Goals, _clause, _run

WAIT_SEC = 5
STEP = {"atom": "web.search", "concept": "ставка ЦБ"}


def _decision(*goals: str, steps: tuple = (STEP,)) -> Decision:
    return Decision(objective="получить данные", reason="уровень 1", action="execute", goals=goals, raw_steps=steps)


class Meeting(Goals):
    """Часть цели, которая ждёт у барьера второго участника: вместе — идут, по очереди — барьер ломается."""

    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__()
        self.barrier = barrier

    def __call__(self, clauses):
        self.barrier.wait()
        return super().__call__(clauses)


def test_goal_parts_and_steps_of_one_move_run_at_the_same_time(base, monkeypatch) -> None:
    barrier = threading.Barrier(2, timeout=WAIT_SEC)
    run = _run(base, Meeting(barrier), _clause("g1", "SALES_REVENUE"))

    def steps(decision) -> MoveOutcome:
        barrier.wait()       # BrokenBarrierError, если части цели ещё не начались: фаза шагов идёт после них
        return MoveOutcome("✓ 1 новых результатов", "✓ web.search")

    monkeypatch.setattr(run, "_run_steps", steps)

    outcome = run._move(_decision("g1"))

    assert not outcome.failed, outcome.details
    assert run.goal_status["g1"] == GOAL_OBTAINED
    assert "web.search" in outcome.details


def test_an_exception_in_the_steps_is_a_failure_with_a_trace_and_the_parts_still_finish(base, monkeypatch) -> None:
    run = _run(base, Goals(), _clause("g1", "SALES_REVENUE"))

    def boom(decision) -> MoveOutcome:
        raise RuntimeError("граф шагов упал")

    monkeypatch.setattr(run, "_run_steps", boom)

    outcome = run._move(_decision("g1"))

    assert outcome.failed
    assert "RuntimeError: граф шагов упал" in outcome.details
    assert "трейс:" in outcome.details and "boom" in outcome.details
    assert run.goal_status["g1"] == GOAL_OBTAINED


def test_the_text_of_a_failed_part_carries_the_tail_of_the_trace(base) -> None:
    class Raising(Goals):
        def __call__(self, clauses):
            raise CaseConflictError(expected=1, actual=2)

    run = _run(base, Raising(), _clause("g1", "SALES_REVENUE"), _clause("g2", "EBITDA"))

    result = run._run_goal_refs(("g1", "g2"))

    assert not result.ok
    assert "CaseConflictError" in result.summary
    assert "трейс:" in result.summary and "__call__" in result.summary


def test_an_unexpected_exception_of_the_whole_move_is_returned_as_text_not_raised(base, monkeypatch) -> None:
    run = _run(base, Goals(), _clause("g1", "SALES_REVENUE"))

    def explode(decision):
        raise ValueError("внутренний сбой хода")

    monkeypatch.setattr(run, "_move", explode)

    outcome = run.move(_decision("g1"))

    assert outcome.failed
    assert "ValueError: внутренний сбой хода" in outcome.details
    assert "трейс:" in outcome.details


def test_a_budget_stop_is_still_raised_out_of_the_move(base, monkeypatch) -> None:
    run = _run(base, Goals(), _clause("g1", "SALES_REVENUE"))

    def over(decision):
        raise BudgetExceeded("лимит")

    monkeypatch.setattr(run, "_move", over)

    with pytest.raises(BudgetExceeded):
        run.move(_decision("g1"))
