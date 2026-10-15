"""Ревью части 14 доводки: граница «ошибка / данных нет» на видах цели с отчётом (Р14.2).

``full_analysis`` по документу, где не нашлось ни одного показателя: узлы чтения отработали и вернули
пусто, ``out.narrative`` честно вернул пусто (нечего рассказывать). Узел ``out.report`` на пустом входе
бросал ``ValueError`` — «данных нет» оформлялось как сбой узла и уходило в цикл, а не пользователю.

Граница остаётся типизированной по входам узла: нарратива нет, а доказательства есть (факты, суждения) —
это по-прежнему сбой узла, то есть ошибка исполнения.
"""
# ruff: noqa: F811 — фикстуры подключены импортом, как в соседних тестах
from __future__ import annotations

import pytest

from finengine.contracts import EvidenceBundleV2, GoalIR, Judgment, NodeKey, ScalarFact
from finengine.skills.atoms.out_report import out_report_executor
from finengine.skills.spec.graph_executor import ExecutionContext, RunInputs

import test_planning_failure_entry as entry
from test_execution_failure_entry import read_value, script  # noqa: F401 — фикстуры
from test_planning_failure_entry import _route

spy = entry.spy
budgets = entry.budgets

_UPSTREAM = NodeKey(atom_name="upstream", atom_version="1", args_fingerprint="x")


def full_analysis() -> GoalIR:
    return GoalIR(raw_query="анализ", intent_kind="full_analysis", role="risks")


def _context(*evidence) -> ExecutionContext:
    return ExecutionContext(
        run_id="run", run_passport=None, inputs=RunInputs(document_text=""),
        evidence_by_key={_UPSTREAM: tuple(evidence)}, requires=(_UPSTREAM,),
    )


def _fact() -> ScalarFact:
    return ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1", source_ref="x", concept="SALES_REVENUE",
        period_ref="2024", value=1.0, unit="RUB_MLN",
    )


def test_a_report_with_nothing_to_report_is_an_empty_result_not_a_failed_node() -> None:
    assert out_report_executor(None, _context()) == []
    assert out_report_executor(None, _context(EvidenceBundleV2(run_id="run"))) == []


@pytest.mark.parametrize("evidence", [
    pytest.param([EvidenceBundleV2(run_id="run", scalar_facts=[_fact()])], id="facts-without-narrative"),
    pytest.param([Judgment(
        concept="SALES_REVENUE", verdict="v", supporting_evidence_ids=["e"], methodology_ref="m",
    )], id="judgment-without-narrative"),
])
def test_a_report_without_a_narrative_over_existing_evidence_is_still_a_failed_node(evidence) -> None:
    with pytest.raises(ValueError, match="NarrativeOutput"):
        out_report_executor(None, _context(*evidence))


def test_an_analysis_of_a_document_without_data_is_a_result_not_a_cycle(base, script, spy, budgets) -> None:
    script.empty = True
    events: list[dict] = []

    outcome = _route(base, full_analysis(), events=events)

    assert not spy.calls, "Р14.2: «данных нет» — результат, цикла нет"
    error = outcome.manifest["error"]
    assert error["code"] == "canonical_pipeline_incomplete" and error["retryable"] is False, error
    assert [e["error"]["code"] for e in events if e.get("type") == "typed_error"] == [error["code"]]


def test_an_analysis_whose_node_failed_still_enters_the_cycle(base, script, spy, budgets) -> None:
    script.fail_first = frozenset({"SALES_REVENUE", "NET_INCOME", "EBITDA"})

    _route(base, full_analysis())

    assert len(spy.calls) == 1 and spy.calls[0]["entry_stage"] == "execution"
