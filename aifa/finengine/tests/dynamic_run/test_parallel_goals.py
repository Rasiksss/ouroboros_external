"""Независимые части цели одного хода исполняются одновременно (ADR-0088).

Прогон 26.10: три независимые части (выручка, EBITDA, чистый долг) шли по очереди — 5,7 минуты первого хода, хотя
каждая части свой граф и берёт готовое из Case. Зависимые части по-прежнему идут после своих зависимостей.
"""
from __future__ import annotations

import contextvars
import threading

from finengine.contracts import GoalIR
from finengine.contracts.intent import GoalClause, TargetNode
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.spec.dynamic_run import GOAL_OBTAINED, DynamicRun, ScenarioResult
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.target import Target
from test_ephemeral_in_graph import CASE, CELL, ENTITY, OWNER

WAIT_SEC = 5
TRACE = contextvars.ContextVar("trace", default="нет")


def _clause(ref: str, concept: str, *, kind: str = "extract_registered_metric", depends_on=()) -> GoalClause:
    return GoalClause(
        goal_ref=ref, raw_query=f"{kind} {concept}", intent_kind=kind,
        targets=[TargetNode(concept=concept, output_kind="scalar")], depends_on=list(depends_on),
    )


class Goals:
    """Канал исполнения частей: записывает, когда часть началась и закончилась, и в каком потоке."""

    def __init__(
        self, *, rendezvous: int = 0, only: frozenset[str] | None = None, failing: frozenset[str] = frozenset(),
    ) -> None:
        self.events: list[tuple[str, str]] = []
        self.threads: dict[str, int] = {}
        self.seen_trace: dict[str, str] = {}
        self._barrier = threading.Barrier(rendezvous, timeout=WAIT_SEC) if rendezvous else None
        self._only = only
        self._failing = failing
        self._lock = threading.Lock()

    def refusal(self, clause) -> None:
        return None

    def __call__(self, clauses) -> ScenarioResult:
        ref = clauses[0].goal_ref
        with self._lock:
            self.events.append(("start", ref))
            self.threads[ref] = threading.get_ident()
            self.seen_trace[ref] = TRACE.get()
        if self._barrier is not None and (self._only is None or ref in self._only):
            self._barrier.wait()      # BrokenBarrierError, если вторая часть не началась одновременно
        with self._lock:
            self.events.append(("end", ref))
        if ref in self._failing:
            return ScenarioResult(False, f"✗ metric_not_found: {ref}")
        return ScenarioResult(True, f"✓ {ref}")


def _run(base, goals: Goals, *clauses: GoalClause) -> DynamicRun:
    goal = GoalIR(raw_query="показатели", intent_kind="custom_analysis")
    run = DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE, goal=goal,
        target=Target(output_type="NarrativeOutput", entity_ref=ENTITY), inputs=RunInputs(),
        run_passport=build_run_passport(
            "financial report", goal_ir_id=goal.goal_id, classification=classification_from_cell(CELL),
        ),
        entry_stage="agent", run_goals=goals,
    )
    for clause in clauses:
        run.clauses[clause.goal_ref] = clause
    return run


def test_independent_parts_of_one_move_run_at_the_same_time(base) -> None:
    goals = Goals(rendezvous=3)
    run = _run(base, goals, _clause("g1", "SALES_REVENUE"), _clause("g2", "OPERATING_PROFIT"), _clause("g3", "CASH_EQUIVALENTS"))

    result = run._run_goal_refs(("g1", "g2", "g3"))

    assert result.ok, result.summary
    assert len({goals.threads[ref] for ref in ("g1", "g2", "g3")}) == 3, "каждая часть — в своём потоке"
    assert all(run.goal_status[ref] == GOAL_OBTAINED for ref in ("g1", "g2", "g3"))


def test_the_summary_keeps_the_order_of_the_parts_whatever_finishes_first(base) -> None:
    goals = Goals(rendezvous=2)
    run = _run(base, goals, _clause("g1", "SALES_REVENUE"), _clause("g2", "OPERATING_PROFIT"))

    result = run._run_goal_refs(("g1", "g2"))

    assert result.summary.splitlines() == ["g1: ✓ g1", "g2: ✓ g2"]


def test_a_part_waits_for_the_part_it_depends_on(base) -> None:
    goals = Goals()
    run = _run(
        base, goals,
        _clause("g1", "SALES_REVENUE"), _clause("g2", "EBITDA", kind="calculate_registered_metric", depends_on=["g1"]),
        _clause("g3", "OPERATING_PROFIT"),
    )

    run._run_goal_refs(("g1", "g2", "g3"))

    assert goals.events.index(("end", "g1")) < goals.events.index(("start", "g2"))


def test_independent_parts_do_not_wait_for_a_slow_dependency_chain(base) -> None:
    """g3 не зависит ни от кого и идёт вместе с g1, а не после цепочки g1 → g2."""
    goals = Goals(rendezvous=2, only=frozenset({"g1", "g3"}))
    run = _run(
        base, goals,
        _clause("g1", "SALES_REVENUE"), _clause("g2", "EBITDA", kind="calculate_registered_metric", depends_on=["g1"]),
        _clause("g3", "OPERATING_PROFIT"),
    )

    result = run._run_goal_refs(("g1", "g2", "g3"))

    assert result.ok, result.summary
    assert goals.threads["g1"] != goals.threads["g3"]


def test_a_part_whose_dependency_failed_is_not_run_and_says_so(base) -> None:
    goals = Goals(failing=frozenset({"g1"}))
    run = _run(
        base, goals,
        _clause("g1", "SALES_REVENUE"), _clause("g2", "EBITDA", kind="calculate_registered_metric", depends_on=["g1"]),
    )

    result = run._run_goal_refs(("g1", "g2"))

    assert not result.ok
    assert ("start", "g2") not in goals.events
    assert "✗ не исполнялась: часть g1 не получена" in result.summary
    assert run.goal_status["g1"].startswith("отказ по ссылке")


def test_a_failed_part_does_not_stop_its_independent_neighbours(base) -> None:
    goals = Goals(failing=frozenset({"g1"}))
    run = _run(base, goals, _clause("g1", "SALES_REVENUE"), _clause("g2", "OPERATING_PROFIT"))

    run._run_goal_refs(("g1", "g2"))

    assert run.goal_status["g2"] == GOAL_OBTAINED


def test_the_attempts_of_every_part_are_recorded_after_a_concurrent_run(base) -> None:
    goals = Goals(rendezvous=2, failing=frozenset({"g1"}))
    run = _run(base, goals, _clause("g1", "BANK_DEPOSITS"), _clause("g2", "SALES_REVENUE"))

    run._run_goal_refs(("g1", "g2"))

    assert run.metric_attempts == {"BANK_DEPOSITS": {"extract"}, "SALES_REVENUE": {"extract"}}


def test_the_context_of_the_move_reaches_the_threads_of_the_parts(base) -> None:
    goals = Goals(rendezvous=2)
    run = _run(base, goals, _clause("g1", "SALES_REVENUE"), _clause("g2", "OPERATING_PROFIT"))
    token = TRACE.set("ход-42")
    try:
        run._run_goal_refs(("g1", "g2"))
    finally:
        TRACE.reset(token)

    assert goals.seen_trace == {"g1": "ход-42", "g2": "ход-42"}


def test_an_error_in_one_part_is_a_refusal_of_that_part_only(base) -> None:
    """ADR-0090: прежде ошибка части выходила из хода (ADR-0088) и роняла ouroboros; теперь это отказ части."""

    class Boom(Goals):
        def __call__(self, clauses):
            if clauses[0].goal_ref == "g2":
                raise RuntimeError("граф упал")
            return super().__call__(clauses)

    run = _run(base, Boom(), _clause("g1", "SALES_REVENUE"), _clause("g2", "OPERATING_PROFIT"))

    result = run._run_goal_refs(("g1", "g2"))

    assert not result.ok
    assert "g2: ✗ RuntimeError: граф упал" in result.summary
    assert run.goal_status["g1"] == GOAL_OBTAINED


def test_a_single_part_runs_in_the_calling_thread(base) -> None:
    goals = Goals()
    run = _run(base, goals, _clause("g1", "SALES_REVENUE"))

    run._run_goal_refs(("g1",))

    assert goals.threads["g1"] == threading.get_ident()
