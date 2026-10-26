"""Статус каждого показателя в состоянии цикла (ADR-0083): найден ли движком, посчитан ли и чего не хватило.

Прогон 24–26.10.2026: корень из итога хода «NET_DEBT не найден» сам выводил, что не хватило BANK_DEPOSITS, и
выбирал способ переделки шага по догадке. Теперь состояние говорит это строкой.
"""
from __future__ import annotations

from types import SimpleNamespace

from finengine.contracts import GoalIR
from finengine.contracts.intent import GoalClause, TargetNode
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.metric_schema import registry_for_profile
from finengine.skills.spec.dynamic_run import DynamicRun, ScenarioResult, render_state
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.graph_planner import AtomStep
from finengine.skills.spec.metric_status import (
    CALC, EXTRACT, metric_status_lines, produced_from_rows, record_attempts, record_step_attempts, scope_enums,
)
from finengine.skills.spec.target import Target
from test_existing_atom_planning import _execute, _give_up, _run as _planned_run
from test_ephemeral_in_graph import CASE, CELL, ENTITY, OWNER

REGISTRY = registry_for_profile(CELL)
FOUND = {"extracted": {"FY 2024", "FY 2025"}, "calculated": set()}
CALCULATED = {"extracted": set(), "calculated": {"FY 2024", "FY 2025"}}


def _line(lines: list[str], enum: str) -> str:
    return next(line for line in lines if line.startswith(f"- {enum} "))


def test_an_extracted_metric_is_marked_found_with_its_periods() -> None:
    lines = metric_status_lines(REGISTRY, ["SALES_REVENUE"], {"SALES_REVENUE": FOUND}, {})

    assert "извлечение — найден (FY 2024, FY 2025)" in _line(lines, "SALES_REVENUE")


def test_a_metric_the_engine_tried_to_extract_and_did_not_get_is_marked_not_found() -> None:
    lines = metric_status_lines(REGISTRY, ["BANK_DEPOSITS"], {}, {"BANK_DEPOSITS": {EXTRACT}})

    assert "извлечение — НЕ НАЙДЕН" in _line(lines, "BANK_DEPOSITS")


def test_a_metric_nobody_asked_for_is_not_called_missing() -> None:
    lines = metric_status_lines(REGISTRY, ["BANK_DEPOSITS"], {}, {})

    assert "извлечение — не пробовали" in _line(lines, "BANK_DEPOSITS")
    assert "НЕ НАЙДЕН" not in _line(lines, "BANK_DEPOSITS")


def test_a_calculated_metric_is_marked_calculated_with_its_periods() -> None:
    lines = metric_status_lines(REGISTRY, ["EBITDA"], {"EBITDA": CALCULATED}, {"EBITDA": {CALC}})

    assert "расчёт — рассчитан (FY 2024, FY 2025)" in _line(lines, "EBITDA")


def test_a_failed_calculation_names_the_components_that_have_no_value() -> None:
    produced = {"CASH_EQUIVALENTS": FOUND, "TOTAL_DEBT": CALCULATED}
    attempts = {"NET_DEBT": {CALC}, "BANK_DEPOSITS": {EXTRACT}, "CASH_EQUIVALENTS": {EXTRACT}, "TOTAL_DEBT": {CALC}}

    lines = metric_status_lines(REGISTRY, ["NET_DEBT"], produced, attempts)

    assert "расчёт — НЕ РАССЧИТАН: нет значений: BANK_DEPOSITS" in _line(lines, "NET_DEBT")
    assert "CASH_EQUIVALENTS" not in _line(lines, "NET_DEBT").split("нет значений:", 1)[1]
    assert "извлечение — НЕ НАЙДЕН" in _line(lines, "BANK_DEPOSITS"), "компонент показан собственной строкой"


def test_a_missing_component_of_a_component_is_named_down_to_the_value_that_is_absent() -> None:
    produced = {"CASH_EQUIVALENTS": FOUND, "BANK_DEPOSITS": FOUND, "LONG_TERM_DEBT": FOUND}
    attempts = {"NET_DEBT": {CALC}, "TOTAL_DEBT": {CALC}, "SHORT_TERM_BORROWINGS": {EXTRACT}}

    lines = metric_status_lines(REGISTRY, ["NET_DEBT"], produced, attempts)

    net_debt = _line(lines, "NET_DEBT")
    assert "НЕ РАССЧИТАН" in net_debt
    assert "SHORT_TERM_BORROWINGS" in net_debt and "LONG_TERM_DEBT" not in net_debt.split("нет значений:", 1)[1]
    assert "TOTAL_DEBT" not in net_debt.split("нет значений:", 1)[1], "названо отсутствующее значение, не промежуточный итог"


def test_a_calculation_nobody_tried_is_not_called_failed() -> None:
    lines = metric_status_lines(REGISTRY, ["NET_DEBT"], {}, {})

    assert "расчёт — не пробовали" in _line(lines, "NET_DEBT")
    assert "НЕ РАССЧИТАН" not in _line(lines, "NET_DEBT")


def test_a_metric_has_exactly_the_registry_words_for_what_it_cannot_do() -> None:
    lines = metric_status_lines(REGISTRY, ["NET_DEBT", "BANK_DEPOSITS"], {}, {})

    assert "извлечение — не извлекается (нет среди «Извлекаемых»)" in _line(lines, "NET_DEBT")
    assert "расчёт — формулы реестра нет" in _line(lines, "BANK_DEPOSITS")


def test_a_metric_outside_the_registry_is_marked_so_and_judged_by_its_values() -> None:
    lines = metric_status_lines(REGISTRY, ["BRENT_PRICE"], {}, {"BRENT_PRICE": {EXTRACT}})

    assert "- BRENT_PRICE (вне реестра): извлечение — НЕ НАЙДЕН" in _line(lines, "BRENT_PRICE")


def test_only_extracted_and_calculated_metrics_count_as_values() -> None:
    finding = SimpleNamespace(output_type="VerificationFinding", concept="BANK_DEPOSITS", period_ref="FY 2025")
    extracted = SimpleNamespace(output_type="ExtractedMetric", concept="bank_deposits", period_ref="FY 2024")
    calculated = SimpleNamespace(output_type="CalculatedMetric", concept="TOTAL_DEBT", period_ref="FY 2024")

    produced = produced_from_rows([{"payloads": [finding, extracted, calculated]}])

    assert produced == {
        "BANK_DEPOSITS": {"extracted": {"FY 2024"}, "calculated": set()},
        "TOTAL_DEBT": {"extracted": set(), "calculated": {"FY 2024"}},
    }


def test_attempting_a_calculation_marks_its_components_as_tried_too() -> None:
    attempts: dict[str, set[str]] = {}
    clause = GoalClause(
        goal_ref="g2", raw_query="Рассчитать NET_DEBT", intent_kind="calculate_registered_metric",
        targets=[TargetNode(concept="NET_DEBT", output_kind="scalar")],
    )

    record_attempts(attempts, REGISTRY, clause)

    assert attempts["NET_DEBT"] == {CALC}
    assert attempts["BANK_DEPOSITS"] == {EXTRACT}
    assert attempts["TOTAL_DEBT"] == {CALC}
    assert attempts["LONG_TERM_DEBT"] == {EXTRACT}


def _run(base, *, with_passport: bool = True, run_goals=None) -> DynamicRun:
    goal = GoalIR(raw_query="чистый долг", intent_kind="custom_analysis")
    passport = build_run_passport(
        "financial report", goal_ir_id=goal.goal_id, classification=classification_from_cell(CELL),
    ) if with_passport else None
    run = DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE, goal=goal,
        target=Target(output_type="NarrativeOutput", entity_ref=ENTITY), inputs=RunInputs(),
        run_passport=passport, entry_stage="agent", run_goals=run_goals,
    )
    run.clauses["g2"] = GoalClause(
        goal_ref="g2", raw_query="Рассчитать NET_DEBT", intent_kind="calculate_registered_metric",
        targets=[TargetNode(concept="NET_DEBT", output_kind="scalar")],
    )
    return run


def _status_block(state: str) -> str:
    return state.split("## Статус показателей\n", 1)[1].split("\n\n## ", 1)[0]


class _Goals:
    """Канал исполнения частей: расчёт упал, как у движка, когда в отчёте нет депозитов."""

    def refusal(self, clause) -> None:
        return None

    def __call__(self, clauses) -> ScenarioResult:
        return ScenarioResult(False, "✗ metric_not_found: Показатель NET_DEBT не найден в отчёте.")


def test_the_state_carries_the_metric_status_section_after_the_registry(base) -> None:
    state = render_state(_run(base), "нет пути")

    assert state.index("## Реестр показателей") < state.index("## Статус показателей") < state.index("## Каталог атомов")
    assert "- NET_DEBT (" in _status_block(state)


def test_a_goal_that_failed_by_reference_leaves_the_failed_status_in_the_state(base) -> None:
    run = _run(base, run_goals=_Goals())

    run._run_goal_refs(("g2",))
    block = _status_block(render_state(run, run.feedback))

    assert "расчёт — НЕ РАССЧИТАН: нет значений:" in _line(block.splitlines(), "NET_DEBT")
    assert "извлечение — НЕ НАЙДЕН" in _line(block.splitlines(), "BANK_DEPOSITS")


def test_without_a_report_profile_the_status_is_reported_as_unavailable(base) -> None:
    assert "профиль отчёта не зафиксирован" in _status_block(render_state(_run(base, with_passport=False), ""))


def _step(atom: str, output_type: str, concept: str | None) -> AtomStep:
    return AtomStep(atom=atom, output_type=output_type, concept=concept)


def test_a_read_step_that_returned_nothing_still_counts_as_an_extraction_attempt() -> None:
    attempts: dict[str, set[str]] = {}

    record_step_attempts(attempts, REGISTRY, [_step("read.value", "ExtractedMetric", "bank_deposits")])

    assert attempts == {"BANK_DEPOSITS": {EXTRACT}}


def test_a_locate_step_is_the_first_stage_of_an_extraction_attempt() -> None:
    attempts: dict[str, set[str]] = {}

    record_step_attempts(attempts, REGISTRY, [_step("locate", "CandidateLocator", "BANK_DEPOSITS")])

    assert attempts == {"BANK_DEPOSITS": {EXTRACT}}


def test_a_calculation_step_marks_the_formula_components_as_tried_like_a_calculation_goal() -> None:
    attempts: dict[str, set[str]] = {}

    record_step_attempts(attempts, REGISTRY, [_step("calc.formula", "CalculatedMetric", "NET_DEBT")])

    assert attempts["NET_DEBT"] == {CALC}
    assert attempts["TOTAL_DEBT"] == {CALC}
    assert attempts["BANK_DEPOSITS"] == {EXTRACT}


def test_steps_without_a_metric_do_not_leave_attempts() -> None:
    attempts: dict[str, set[str]] = {}

    record_step_attempts(attempts, REGISTRY, [
        _step("read.quote", "Quote", None), _step("eph.x", "dyn:x", "BANK_DEPOSITS"), _step("out.file", "FileArtifact", "NET_DEBT"),
    ])

    assert attempts == {}


def test_the_status_lists_what_the_goals_named_and_what_only_steps_tried() -> None:
    clause = GoalClause(
        goal_ref="g1", raw_query="Извлечь SALES_REVENUE", intent_kind="extract_registered_metric",
        targets=[TargetNode(concept="SALES_REVENUE", output_kind="scalar")],
    )
    attempts = {"BANK_DEPOSITS": {EXTRACT}, "SALES_REVENUE": {EXTRACT}}

    assert scope_enums([clause], attempts) == ["SALES_REVENUE", "BANK_DEPOSITS"]


def test_a_metric_tried_only_by_steps_gets_its_own_line_in_the_state(base) -> None:
    run, _ = _planned_run(base, [_execute({"atom": "read.value", "concept": "BANK_DEPOSITS"}), _give_up()])

    run.run(deterministic_error="нет пути")
    block = _status_block(render_state(run, run.feedback))

    assert "извлечение — НЕ НАЙДЕН" in _line(block.splitlines(), "BANK_DEPOSITS")
