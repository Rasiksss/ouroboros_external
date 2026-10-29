"""Параллельные части одного хода не гоняются за голову Case (ADR-0090).

Прогон 26.10 (db4edbcd): три части хода одновременно читали ревизию Case, первая записала план и подняла ревизию,
остальные получили `CaseConflictError`; ошибка вышла из потока части, ход дал 500 и уронил поток `agent-runtime`
в ouroboros. Теперь планирование частей одного Case идёт по очереди (дёшево: граф собирается без модели),
исполнение остаётся параллельным, а сбой части возвращается планировщику отказом этой части.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from finengine.contracts import AdHocConceptSpec, GoalIR
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.case_runtime import CaseRuntime
from finengine.skills.spec.dynamic_run import GOAL_OBTAINED
from finengine.skills.spec.target import Target
from finengine.store.cases import CaseConflictError
from test_ephemeral_in_graph import CASE, CELL, ENTITY, OWNER
from test_parallel_goals import Goals, _clause, _run

PLAN_PAUSE_SEC = 0.3


class Raising(Goals):
    """Канал исполнения, где названные части падают исключением, как в прогоне 26.10."""

    def __init__(self, bad: frozenset[str]) -> None:
        super().__init__()
        self.bad = bad

    def __call__(self, clauses):
        if clauses[0].goal_ref in self.bad:
            raise CaseConflictError(expected=1, actual=2)
        return super().__call__(clauses)


def _plan(runtime: CaseRuntime, concept: str):
    goal = GoalIR(raw_query=concept, intent_kind="extract_registered_metric")
    passport = build_run_passport(
        "financial report", goal_ir_id=goal.goal_id, classification=classification_from_cell(CELL))
    return runtime.plan(
        owner_id=OWNER, case_id=CASE, goal_ir=goal, run_passport=passport,
        target=Target(output_type="ExtractedMetric", concept_id=concept, entity_ref=ENTITY, period_ref="2024"),
    )


def _slow_planner(runtime: CaseRuntime, monkeypatch, on_read=None) -> None:
    """Пауза внутри планировщика: ревизию Case часть уже прочитала, а голову ещё не записала."""
    real = runtime.planner.plan_ex

    def slow(*args, **kwargs):
        if on_read is not None:
            on_read.set()
        time.sleep(PLAN_PAUSE_SEC)
        return real(*args, **kwargs)

    monkeypatch.setattr(runtime.planner, "plan_ex", slow)


def test_plans_of_parts_of_one_case_are_built_one_after_another(base, monkeypatch) -> None:
    _slow_planner(base, monkeypatch)
    start = threading.Barrier(3, timeout=5)

    def part(concept: str):
        start.wait()
        return _plan(base, concept).record.plan_run_id

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(part, concept) for concept in ("EBITDA", "REVENUE", "EBITDA")]
        plan_ids = [future.result() for future in futures]       # CaseConflictError поднимется здесь

    assert len(plan_ids) == 3
    assert base.store.load_case(OWNER, CASE).revision == 4      # 1 — документ, по одной на каждый план


def test_registering_an_ad_hoc_spec_does_not_break_a_plan_of_another_part(base, monkeypatch) -> None:
    reading = threading.Event()
    _slow_planner(base, monkeypatch, on_read=reading)
    spec = AdHocConceptSpec(term="торговая площадь")

    with ThreadPoolExecutor(max_workers=2) as pool:
        planning = pool.submit(_plan, base, "EBITDA")
        assert reading.wait(5)
        registering = pool.submit(base.register_ad_hoc_spec, OWNER, CASE, spec)   # ревизия растёт посреди плана
        registering.result()
        planning.result()                                                       # CaseConflictError поднимется здесь


def test_the_lock_of_a_case_is_shared_and_reentrant() -> None:
    from finengine.store.cases import case_lock

    first = case_lock(OWNER, CASE)

    assert case_lock(OWNER, CASE) is first
    assert case_lock(OWNER, "other-case") is not first
    with first, first:
        pass


def test_a_part_that_raises_becomes_a_refusal_of_that_part_and_the_others_finish(base) -> None:
    goals = Raising(frozenset({"g1"}))
    run = _run(base, goals, _clause("g1", "SALES_REVENUE"), _clause("g2", "EBITDA"), _clause("g3", "NET_DEBT"))

    result = run._run_goal_refs(("g1", "g2", "g3"))

    assert not result.ok
    assert "CaseConflictError" in result.summary
    assert run.goal_status["g2"] == GOAL_OBTAINED
    assert run.goal_status["g3"] == GOAL_OBTAINED
    assert run.goal_status["g1"] != GOAL_OBTAINED


def test_a_budget_stop_is_not_swallowed_by_a_part(base) -> None:
    from finengine.llm.budget import BudgetExceeded

    class Over(Goals):
        def __call__(self, clauses):
            raise BudgetExceeded("лимит")

    # цикл обязан увидеть остановку по бюджету, а не отказ части
    run = _run(base, Over(), _clause("g1", "SALES_REVENUE"), _clause("g2", "EBITDA"))
    try:
        run._run_goal_refs(("g1", "g2"))
    except BudgetExceeded:
        return
    raise AssertionError("BudgetExceeded проглочен")
