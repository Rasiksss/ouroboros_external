"""Реестр показателей в состоянии планировщика: что читается из отчёта и что считается формулой реестра.

Прогон владельца 07.10.2026: «посчитай EBITDA» планировщик превратил в сценарий «Извлеки EBITDA», получил
«не найден» и пошёл искать показатель шагами — о том, что EBITDA считается формулой реестра, он не знал.
"""
from __future__ import annotations

from finengine.contracts import GoalIR
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.spec.dynamic_run import DynamicRun, render_state
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.target import Target
from test_ephemeral_in_graph import CASE, CELL, ENTITY, OWNER


def _state(base, *, role: str | None = None, with_passport: bool = True) -> str:
    goal = GoalIR(raw_query="посчитай EBITDA и корреляцию с Brent", intent_kind="custom_analysis", role=role)
    passport = build_run_passport(
        "financial report", goal_ir_id=goal.goal_id, classification=classification_from_cell(CELL),
    ) if with_passport else None
    dynamic = DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE, goal=goal,
        target=Target(output_type="NarrativeOutput", entity_ref=ENTITY), inputs=RunInputs(),
        run_passport=passport,
    )
    return render_state(dynamic, "нет пути")


def _registry_block(state: str) -> str:
    return state.split("## Реестр показателей\n", 1)[1].split("\n\n## ", 1)[0]


def test_the_planner_sees_which_metrics_are_read_and_which_are_calculated(base) -> None:
    block = _registry_block(_state(base))
    extractable, calculated = block.split("Рассчитываемые", 1)

    assert "- OPERATING_PROFIT — " in extractable
    assert "- EBITDA — " in extractable, "EBITDA читается, если напечатана в отчёте"
    assert "= OPERATING_PROFIT + DEPRECIATION_AMORTIZATION" in calculated
    assert "- EBITDA_MARGIN — " in calculated and "- EBITDA_MARGIN — " not in extractable


def test_the_role_form_metrics_are_named_for_a_role_with_a_form(base) -> None:
    assert "Форма роли" in _registry_block(_state(base, role="die"))
    assert "Форма роли" not in _registry_block(_state(base, role="other"))


def test_without_a_report_profile_the_registry_is_reported_as_unavailable(base) -> None:
    assert "реестр недоступен" in _registry_block(_state(base, with_passport=False))
