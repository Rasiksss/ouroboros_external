"""Ревью части 14 доводки: находки, закреплённые тестами (каждый без исправления красный).

Падение исполнителя исключением (таймаут, бюджет вызовов) не должно терять то, что узлы уже успели
получить: без этого цикл извлекает заново то, что первая попытка уже дала (Р14, «Что сделать», п. 2).
"""
# ruff: noqa: F811 — фикстуры подключены импортом, как в соседних тестах
from __future__ import annotations

from types import SimpleNamespace

import pytest

from finengine.skills.spec import graph_executor
from finengine.skills.spec.graph_executor import ExecutionBudget

import test_planning_failure_entry as entry
from test_chat_outcome_contract import _arm_dynamic
from test_execution_failure_entry import (  # noqa: F401 — фикстуры
    _finalize_with_metrics,
    force_budget,
    read_value,
    script,
    single_metric,
)
from test_planning_failure_entry import _route

spy = entry.spy
budgets = entry.budgets


@pytest.fixture
def clock_after_first_read(monkeypatch, script):
    """Часы исполнителя: после первого вызова ``read.value`` проходит 1000 с — бюджет в 500 с выходит."""
    monkeypatch.setattr(
        graph_executor, "time", SimpleNamespace(monotonic=lambda: 1000.0 * len(script.calls)),
    )


def _extracted(base) -> list[dict]:
    return [
        row for row in base.store.list_artifacts(entry.OWNER, "chat-case")
        if row["output_type"] == "ExtractedMetric"
    ]


def test_a_crash_after_a_successful_node_keeps_its_result_in_the_case(
    base, script, spy, budgets, force_budget, clock_after_first_read,
) -> None:
    force_budget(ExecutionBudget(max_wall_seconds=500.0, max_retries=0))

    _route(base, single_metric())

    assert len(script.calls) == 1, "read.value отработал до таймаута"
    assert spy.calls and spy.calls[0]["entry_stage"] == "execution"
    assert _extracted(base), "извлечённый показатель остался в Case: цикл его переиспользует"


def test_the_cycle_after_a_crash_sees_the_metric_and_does_not_extract_it_again(
    base, script, budgets, force_budget, clock_after_first_read, monkeypatch,
) -> None:
    force_budget(ExecutionBudget(max_wall_seconds=500.0, max_retries=0))
    planner = _arm_dynamic(monkeypatch, [_finalize_with_metrics])

    outcome = _route(base, single_metric())

    assert "| ExtractedMetric | SALES_REVENUE |" in planner.prompts[0], "планировщик видит уже полученное"
    assert outcome.manifest["dynamic_run"]["status"] == "finalized"
    assert script.requested("SALES_REVENUE") == 1
