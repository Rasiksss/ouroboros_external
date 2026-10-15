"""Часть 13 доводки, ревью: что НЕ входит в цикл.

Правило входа части 13 — сбой ПЛАНИРОВАНИЯ. Граф, который собрался и исполнился, остаётся детерминированным.
Часть 14 (Р14.1) осознанно переписала один случай: собравшийся граф, у которого узел УПАЛ при исполнении,
теперь тоже входит в цикл — см. ``test_execution_failure_entry.py``, где граница ошибка / «данных нет»
проверена по каждому исходу.
"""
from __future__ import annotations

import pytest

from finengine.contracts import GoalIR
import test_planning_failure_entry as entry
from test_planning_failure_entry import _clause, _compound, _route

# фикстуры части 13: подставной `DynamicRun` и бюджет рантайма
spy = entry.spy
budgets = entry.budgets


@pytest.fixture
def failing_read(read_value):
    """`read.value`, у которого узел падает при исполнении, а не при планировании (часть 14)."""
    counted, _ = read_value

    def _failing_read(items, ctx):
        raise RuntimeError("узел упал")

    counted.inner = _failing_read


def single_metric() -> GoalIR:
    return GoalIR(
        raw_query="выручка", intent_kind="extract_registered_metric",
        targets=[{"concept": "SALES_REVENUE", "output_kind": "scalar"}],
    )


def two_independent_metrics() -> GoalIR:
    return _compound(
        _clause("goal-1", "extract_registered_metric", concept="SALES_REVENUE"),
        _clause("goal-2", "extract_registered_metric", concept="NET_INCOME"),
    )


def dependent_extract_and_calculation() -> GoalIR:
    return _compound(
        _clause("goal-1", "extract_registered_metric", concept="SALES_REVENUE"),
        _clause("goal-2", "calculate_registered_metric", concept="EBITDA_MARGIN", depends_on=["goal-1"]),
    )


@pytest.mark.parametrize(
    "make_goal", [single_metric, two_independent_metrics, dependent_extract_and_calculation],
)
def test_a_planned_graph_that_fails_while_running_is_a_cycle_entry_since_part_14(
    make_goal, base, spy, budgets, failing_read,
) -> None:
    _route(base, make_goal())

    assert len(spy.calls) == 1 and spy.calls[0]["entry_stage"] == "execution", "Р14.1: упавший граф — в цикл"


def test_a_deterministic_compound_goal_with_a_dependency_is_planned_together(base, spy, budgets) -> None:
    """Зависимость между детерминированными частями — рёбра `plan_many`, а не повод для цикла."""
    events: list[dict] = []

    _route(base, dependent_extract_and_calculation(), events=events)

    plans = [event for event in events if event.get("type") == "plan_graph"]
    assert plans and all("compound" in event.get("planner_version", "") for event in plans)
    assert not spy.calls
