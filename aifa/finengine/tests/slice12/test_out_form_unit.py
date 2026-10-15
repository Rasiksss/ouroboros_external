from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from finengine.contracts import CalculatedMetric, DerivedFact, ExtractedMetric, ScalarFact
from finengine.roles.scope import layouts
from finengine.skills.atoms.out_form import build_form_output, out_form_executor
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.spec.atom_registry import NodeArgs


def _first_enum() -> str:
    return next(
        row["enum"]
        for row in layouts()["roles"]["dgr"]["formats"]["bank"]["rows"]
        if row.get("enum")
    )


def test_calculated_metric_is_copied_without_recalculation():
    concept = _first_enum()
    derived = DerivedFact(
        evidence_id="derived-1",
        produced_by_atom="calc.formula",
        produced_by_atom_version="3.2",
        source_ref="formula:already-calculated",
        concept=concept,
        period_ref="2025",
        formula_id="f-1",
        formula_version="3.2",
        inputs=["ev-a", "ev-b"],
        value=17.25,
    )
    form = build_form_output(
        run_id="run-1",
        role="dgr",
        company_type="bank",
        metrics=[CalculatedMetric(derived_fact=derived, concept=concept, period_ref="2025")],
    )
    row = next(row for row in form.rows if row.enum == concept)
    assert row.value == 17.25
    assert row.display_value == "17.25"
    assert row.evidence_refs[0].evidence_id == "derived-1"


def test_conflicting_evidence_is_explicit_and_does_not_choose_a_number():
    concept = _first_enum()
    metrics = [
        ExtractedMetric(
            scalar_fact=ScalarFact(
                evidence_id=f"ev-{value}", produced_by_atom="read.value",
                produced_by_atom_version="1.0", source_ref=f"doc#{value}",
                concept=concept, period_ref="2025", value=value, unit="RUB_MLN",
            ),
            concept=concept,
            period_ref="2025",
        )
        for value in (10.0, 11.0)
    ]
    form = build_form_output(
        run_id="run-1", role="dgr", company_type="bank", metrics=metrics,
    )
    row = next(row for row in form.rows if row.enum == concept)
    assert row.status == "conflicting"
    assert row.value is None and row.missing_reason == "conflicting_evidence"
    assert [ref.evidence_id for ref in row.evidence_refs] == ["ev-10.0", "ev-11.0"]


def test_unknown_role_is_not_silently_replaced_with_another_layout():
    with pytest.raises(ValueError, match="layout is not defined"):
        build_form_output(
            run_id="run-1", role="not-a-role", company_type="bank", metrics=[],
        )


def test_layout_version_comes_from_the_versioned_layout(monkeypatch):
    from finengine.skills.atoms import out_form

    versioned = copy.deepcopy(layouts())
    versioned["version"] = "2.1"
    monkeypatch.setattr(out_form, "layouts", lambda: versioned)
    form = build_form_output(
        run_id="run-1", role="dgr", company_type="bank", metrics=[],
    )
    assert form.layout_version == "2.1"


def test_all_periods_form_keeps_each_comparative_value_as_its_own_row():
    metrics = [
        ExtractedMetric(
            scalar_fact=ScalarFact(
                evidence_id=f"revenue-{period}", produced_by_atom="read.value",
                produced_by_atom_version="1.0", source_ref=f"page:4:{period}",
                concept="SALES_REVENUE", period_ref=period, value=value,
                unit="тыс. руб.",
            ),
            concept="SALES_REVENUE", period_ref=period,
        )
        for period, value in (("6M 2026", 200.0), ("6M 2025", 100.0))
    ]

    form = build_form_output(
        run_id="run-all-periods", role="other", company_type="non_financial",
        metrics=metrics, requested_concepts=("SALES_REVENUE",),
        requested_periods=("6M 2026", "6M 2025"),
    )

    assert form.layout_id == "requested.non_financial.all-periods"
    assert [(row.label, row.value, row.status) for row in form.rows] == [
        ("SALES_REVENUE — 6M 2026", 200.0, "supported"),
        ("SALES_REVENUE — 6M 2025", 100.0, "supported"),
    ]
    assert [row.evidence_refs[0].period_ref for row in form.rows] == ["6M 2026", "6M 2025"]


def test_all_available_axis_comes_from_evidence_not_a_period_prepass():
    metrics = [
        ExtractedMetric(
            scalar_fact=ScalarFact(
                evidence_id=f"revenue-{period}", produced_by_atom="read.value",
                produced_by_atom_version="1.0", source_ref=f"page:4:{period}",
                concept="SALES_REVENUE", period_ref=period, value=value,
                unit="тыс. руб.",
            ),
            concept="SALES_REVENUE", period_ref=period,
        )
        for period, value in (("6M 2026", 200.0), ("6M 2025", 100.0))
    ]

    class _Ctx:
        run_id = "run-evidence-axis"
        run_passport = build_run_passport(
            "report", goal_ir_id="goal", classification=classification_from_cell("ifrs_nf_ru"),
        )
        inputs = SimpleNamespace(role="other")

        @staticmethod
        def dependencies():
            return metrics

    form = out_form_executor(NodeArgs(extra={
        "role": "other", "requested_concepts": "SALES_REVENUE",
        "period_scope": "all_available",
    }), _Ctx())[0]

    assert [(row.label, row.value) for row in form.rows] == [
        ("SALES_REVENUE — 6M 2026", 200.0),
        ("SALES_REVENUE — 6M 2025", 100.0),
    ]
