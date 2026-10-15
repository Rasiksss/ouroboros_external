"""`out.changes` — критерии готовности карточки 32 (T1, T2, T6, T7):

1. Δ % = (текущее − предыдущее) / |предыдущее| × 100, считает код; база 0 → `zero_base`
   (абсолютная разница есть, процента нет).
2. Пара периодов берётся из `ReferenceValue` (`ref.prior`), не выбирается заново.
3. Строка без сравнения не выбрасывается: статус и причина; показатели роли, которых в
   отчёте нет вовсе, — один `OutputGap` со счётчиком.
4. Объяснение — best-effort: `NarrativeOutput` есть → `ready`; текст заблокирован
   `verify.grounding` или модель ничего не вернула → таблица остаётся, `unavailable`
   с названной причиной.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from finengine.contracts import (
    ClassificationResult,
    EntityIdentity,
    EvidenceBundleV2,
    NarrativeOutput,
    PeriodChangeReport,
    ReferenceValue,
    RunPassport,
    ScalarFact,
    VerificationFinding,
)
from finengine.skills.atoms.out_changes import out_changes_executor, scope_concepts
from finengine.skills.spec.atom_registry import NodeArgs

CURRENT, PRIOR = "FY 2025", "FY 2024"


def _passport() -> RunPassport:
    return RunPassport(
        goal_ir_id="goal-1",
        classification=ClassificationResult(
            profile_id="ifrs_nf_ru", standard="ifrs", company_type="non_financial",
            language="ru", reason="test fixture",
        ),
        entity=EntityIdentity(entity_id="ent-1", canonical_name="Тест", raw_name="Тест"),
        registry_version="registry-7", methodology_version="methodology-1",
        model_version="model-1", prompt_version="prompt-1", sandbox_image_version="sandbox-1",
    )


def _fact(concept: str, period: str, value: float, *, unit: str = "млн. руб.") -> ScalarFact:
    return ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0",
        source_ref=f"loc:{concept}:{period}", concept=concept, period_ref=period,
        value=value, unit=unit,
    )


def _reference(current: ScalarFact, prior: ScalarFact) -> ReferenceValue:
    return ReferenceValue(
        concept=current.concept, current_period_ref=current.period_ref,
        current_evidence_id=current.evidence_id, prior_period_ref=prior.period_ref,
        prior_evidence_id=prior.evidence_id, selection_reason="fixture",
    )


@dataclass
class _Inputs:
    role: str = ""


@dataclass
class _Ctx:
    _deps: list
    run_passport: RunPassport | None = field(default_factory=_passport)
    run_id: str = "run-1"
    inputs: _Inputs = field(default_factory=_Inputs)

    def dependencies(self):
        return tuple(self._deps)


def _run(concepts: str, *, facts=(), references=(), extra_deps=(), role: str = "") -> list:
    bundle = EvidenceBundleV2(run_id="run-1", scalar_facts=list(facts))
    args = NodeArgs(extra={"requested_concepts": concepts, "role": role})
    return out_changes_executor(args, _Ctx([bundle, *references, *extra_deps]))


def _only_report(result: list) -> PeriodChangeReport:
    assert len(result) == 1 and isinstance(result[0], PeriodChangeReport)
    return result[0]


def _row(report: PeriodChangeReport, concept: str):
    return next(row for row in report.rows if row.concept == concept)


def _narrative() -> NarrativeOutput:
    return NarrativeOutput(run_id="run-1", text="Выручка выросла за счёт объёмов.")


# --- T2: арифметика ---------------------------------------------------------

@pytest.mark.parametrize(("prior", "current", "absolute", "pct"), [
    (1200.0, 1500.0, 300.0, 25.0),      # рост
    (200.0, 150.0, -50.0, -25.0),       # падение
    (500.0, 500.0, 0.0, 0.0),           # без изменений
    (-100.0, -50.0, 50.0, 50.0),        # отрицательная база: убыток сократился
    (-100.0, -150.0, -50.0, -50.0),     # отрицательная база: убыток вырос
    (100.0, 110.0, 10.0, 10.0),         # без шума float: 10/100*100 ≠ 10.000000000000002
])
def test_percent_change_is_absolute_change_over_the_absolute_prior(prior, current, absolute, pct):
    cur, old = _fact("SALES_REVENUE", CURRENT, current), _fact("SALES_REVENUE", PRIOR, prior)
    row = _row(_only_report(_run(
        "SALES_REVENUE", facts=[cur, old], references=[_reference(cur, old)],
    )), "SALES_REVENUE")

    assert row.status == "computed"
    assert row.absolute_change == absolute
    assert row.relative_change_pct == pct
    assert (row.current_value, row.prior_value) == (current, prior)
    assert (row.current_period_ref, row.prior_period_ref) == (CURRENT, PRIOR)
    assert [ref.evidence_id for ref in row.evidence_refs] == [cur.evidence_id, old.evidence_id]
    assert row.unit == "млн. руб."


def test_zero_prior_keeps_the_absolute_change_and_drops_the_percentage():
    cur, old = _fact("NET_INCOME", CURRENT, 40.0), _fact("NET_INCOME", PRIOR, 0.0)
    row = _row(_only_report(_run(
        "NET_INCOME", facts=[cur, old], references=[_reference(cur, old)],
    )), "NET_INCOME")

    assert row.status == "zero_base"
    assert row.absolute_change == 40.0
    assert row.relative_change_pct is None
    assert "процент не определён" in row.reason


# --- T7: строки без сравнения -----------------------------------------------

def test_a_single_value_without_a_prior_period_is_kept_as_a_row_with_a_reason():
    only = _fact("SALES_REVENUE", CURRENT, 1500.0)
    row = _row(_only_report(_run("SALES_REVENUE", facts=[only])), "SALES_REVENUE")

    assert row.status == "no_prior_period"
    assert row.relative_change_pct is None and row.absolute_change is None
    assert (row.current_value, row.current_period_ref) == (1500.0, CURRENT)
    assert row.prior_value is None
    assert row.reason


def test_several_values_without_a_pair_do_not_invent_which_one_is_current():
    facts = [_fact("SALES_REVENUE", "FY 2023", 1000.0), _fact("SALES_REVENUE", "FY 2022", 900.0)]
    row = _row(_only_report(_run("SALES_REVENUE", facts=facts)), "SALES_REVENUE")

    assert row.status == "no_prior_period"
    assert row.current_value is None and row.current_period_ref is None
    assert len(row.evidence_refs) == 2


def test_an_incomparable_pair_is_reported_with_the_reason_of_ref_prior():
    cur = _fact("SALES_REVENUE", CURRENT, 1500.0)
    old = _fact("SALES_REVENUE", PRIOR, 1200.0, unit="тыс. руб.")
    finding = VerificationFinding(
        produced_by_atom="ref.prior", produced_by_atom_version="1.0.0",
        source_ref=f"{cur.evidence_id},{old.evidence_id}", concept="SALES_REVENUE",
        period_ref=CURRENT, check_kind="unit", status="not_comparable",
        detail="единица предыдущего периода не совпадает",
    )
    row = _row(_only_report(_run(
        "SALES_REVENUE", facts=[cur, old], extra_deps=[finding],
    )), "SALES_REVENUE")

    assert row.status == "not_comparable"
    assert row.reason == "единица предыдущего периода не совпадает"
    assert row.relative_change_pct is None
    assert {ref.evidence_id for ref in row.evidence_refs} == {cur.evidence_id, old.evidence_id}


def test_a_finding_without_detail_still_gives_a_row_with_a_reason_not_a_contract_error():
    cur, old = _fact("SALES_REVENUE", CURRENT, 1500.0), _fact("SALES_REVENUE", PRIOR, 1200.0)
    finding = VerificationFinding(
        produced_by_atom="ref.prior", produced_by_atom_version="1.0.0",
        source_ref=f"{cur.evidence_id},{old.evidence_id}", concept="SALES_REVENUE",
        check_kind="basis", status="not_comparable",
    )
    row = _row(_only_report(_run(
        "SALES_REVENUE", facts=[cur, old], extra_deps=[finding],
    )), "SALES_REVENUE")

    assert row.status == "not_comparable" and row.reason


def test_a_reference_to_evidence_missing_from_the_bundle_is_not_comparable_not_a_crash():
    cur, old = _fact("SALES_REVENUE", CURRENT, 1500.0), _fact("SALES_REVENUE", PRIOR, 1200.0)
    row = _row(_only_report(_run(
        "SALES_REVENUE", facts=[cur], references=[_reference(cur, old)],
    )), "SALES_REVENUE")

    assert row.status == "not_comparable"
    assert "пакете доказательств" in row.reason


def test_role_concepts_absent_from_the_report_are_one_gap_with_a_counter_not_rows():
    cur, old = _fact("SALES_REVENUE", CURRENT, 1500.0), _fact("SALES_REVENUE", PRIOR, 1200.0)
    report = _only_report(_run(
        "SALES_REVENUE,NET_INCOME,EBITDA", facts=[cur, old], references=[_reference(cur, old)],
    ))

    assert [row.concept for row in report.rows] == ["SALES_REVENUE"]
    assert [gap.code for gap in report.gaps] == ["concepts_not_found"]
    assert "2 из 3" in report.gaps[0].message
    assert report.gaps[0].required is False


def test_nothing_to_show_returns_no_report_so_the_gate_can_refuse_honestly():
    assert _run("SALES_REVENUE,NET_INCOME", facts=[]) == []


# --- порядок и определённость ------------------------------------------------

def test_rows_are_ordered_by_status_then_by_the_size_of_the_change_and_are_contiguous():
    def pair(concept, prior, current):
        old, cur = _fact(concept, PRIOR, prior), _fact(concept, CURRENT, current)
        return old, cur, _reference(cur, old)

    small_old, small_cur, small_ref = pair("NET_INCOME", 100.0, 105.0)       # +5 %
    big_old, big_cur, big_ref = pair("SALES_REVENUE", 100.0, 60.0)           # −40 %
    zero_old, zero_cur, zero_ref = pair("EBITDA", 0.0, 10.0)                 # zero_base
    lone = _fact("COSTS_EXPENSES", CURRENT, 70.0)                            # no_prior_period

    report = _only_report(_run(
        "COSTS_EXPENSES,EBITDA,NET_INCOME,SALES_REVENUE",
        facts=[small_old, small_cur, big_old, big_cur, zero_old, zero_cur, lone],
        references=[small_ref, big_ref, zero_ref],
    ))

    assert [row.concept for row in report.rows] == ["SALES_REVENUE", "NET_INCOME", "EBITDA", "COSTS_EXPENSES"]
    assert [row.order for row in report.rows] == [0, 1, 2, 3]


def test_the_report_is_deterministic_for_the_same_evidence():
    cur, old = _fact("SALES_REVENUE", CURRENT, 1500.0), _fact("SALES_REVENUE", PRIOR, 1200.0)
    first = _only_report(_run("SALES_REVENUE", facts=[cur, old], references=[_reference(cur, old)]))
    second = _only_report(_run("SALES_REVENUE", facts=[cur, old], references=[_reference(cur, old)]))

    assert first.rows == second.rows


def test_an_unknown_concept_falls_back_to_its_code_as_the_label():
    cur, old = _fact("ZZZ_UNKNOWN", CURRENT, 2.0), _fact("ZZZ_UNKNOWN", PRIOR, 1.0)
    row = _row(_only_report(_run("ZZZ_UNKNOWN", facts=[cur, old], references=[_reference(cur, old)])), "ZZZ_UNKNOWN")

    assert row.label == "ZZZ_UNKNOWN"


def test_a_registry_concept_gets_a_human_label():
    cur, old = _fact("SALES_REVENUE", CURRENT, 2.0), _fact("SALES_REVENUE", PRIOR, 1.0)
    row = _row(_only_report(_run("SALES_REVENUE", facts=[cur, old], references=[_reference(cur, old)])), "SALES_REVENUE")

    assert row.label and row.label != "SALES_REVENUE"


# --- T6: объяснение — best-effort, таблица — нет ---------------------------

def test_a_grounded_narrative_is_carried_and_marks_the_report_complete():
    cur, old = _fact("SALES_REVENUE", CURRENT, 1500.0), _fact("SALES_REVENUE", PRIOR, 1200.0)
    narrative = _narrative()
    report = _only_report(_run(
        "SALES_REVENUE", facts=[cur, old], references=[_reference(cur, old)], extra_deps=[narrative],
    ))

    assert report.explanation_status == "ready"
    assert report.explanation == narrative
    assert report.explanation_note is None
    assert report.status == "complete"


def test_a_narrative_blocked_by_grounding_leaves_the_table_and_names_the_reason():
    cur, old = _fact("SALES_REVENUE", CURRENT, 1500.0), _fact("SALES_REVENUE", PRIOR, 1200.0)
    blocked = VerificationFinding(
        produced_by_atom="verify.grounding", produced_by_atom_version="1.0.0",
        source_ref="no-evidence", check_kind="grounding", status="fail",
        detail="финансовое утверждение 'выросла на 40%' без evidence_id",
    )
    report = _only_report(_run(
        "SALES_REVENUE", facts=[cur, old], references=[_reference(cur, old)], extra_deps=[blocked],
    ))

    assert report.explanation is None and report.explanation_status == "unavailable"
    assert "заблокировано проверкой опоры" in report.explanation_note
    assert "без evidence_id" in report.explanation_note
    assert report.status == "partial"
    assert _row(report, "SALES_REVENUE").relative_change_pct == 25.0, "таблица не зависит от текста"


def test_a_model_that_returned_nothing_leaves_the_table_and_says_so():
    cur, old = _fact("SALES_REVENUE", CURRENT, 1500.0), _fact("SALES_REVENUE", PRIOR, 1200.0)
    report = _only_report(_run("SALES_REVENUE", facts=[cur, old], references=[_reference(cur, old)]))

    assert report.explanation_status == "unavailable"
    assert "не сформировано" in report.explanation_note
    assert report.status == "partial"


# --- границы -----------------------------------------------------------------

def test_the_atom_refuses_to_run_without_a_frozen_passport():
    with pytest.raises(ValueError, match="RunPassport"):
        out_changes_executor(NodeArgs(extra={"requested_concepts": "SALES_REVENUE"}), _Ctx([], run_passport=None))


def test_the_report_carries_the_role_and_run_metadata_of_the_passport():
    cur, old = _fact("SALES_REVENUE", CURRENT, 1500.0), _fact("SALES_REVENUE", PRIOR, 1200.0)
    report = _only_report(_run(
        "SALES_REVENUE", facts=[cur, old], references=[_reference(cur, old)], role="risks",
    ))

    assert report.role == "risks"
    assert report.run_metadata.registry_version == "registry-7"
    assert report.run_metadata.methodology_version == "methodology-1"


def test_scope_is_the_requested_list_or_else_the_role_scope_of_the_cell():
    passport = _passport()
    assert scope_concepts({"requested_concepts": "NET_INCOME,SALES_REVENUE,NET_INCOME"}, passport) == (
        "NET_INCOME", "SALES_REVENUE",
    )
    role_scope = scope_concepts({"role": "other"}, passport)
    assert len(role_scope) > 20 and list(role_scope) == sorted(role_scope)
    assert "SALES_REVENUE" in role_scope
