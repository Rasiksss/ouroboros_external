"""A0 семантика, которую пустой round-trip не проверяет.

Три нерушимых решения из `01-nerushimye-resheniya.md`, зафиксированные как
тесты ДО реализации:

* решение №2 — классификация фиксируется в `RunPassport` один раз;
* решение №4 — у каждого доказательства есть атом-производитель, его версия,
  источник, и исправление создаёт НОВОЕ доказательство со связью на исходное,
  а не мутирует старое;
* решение №8 — пустая/недоступная строка формы не выбрасывается.

Плюс версии реестра/методики/модели/инструкции/песочницы в `RunPassport`
(A0, второй абзац) и отрицательные сценарии конструирования.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from finengine.contracts import (
    ClassificationResult,
    DerivedFact,
    EntityIdentity,
    FormOutput,
    FormRow,
    NodeKey,
    PeriodAxis,
    RunPassport,
    ScalarFact,
)

_CLASSIFICATION = ClassificationResult(
    profile_id="ifrs_nf_ru",
    standard="ifrs",
    company_type="non_financial",
    language="ru",
    confidence_low=False,
    reason="strict IFRS basis phrase",
)

_ENTITY = EntityIdentity(
    entity_id="ent-test-1",
    canonical_name="ПАО Ромашка",
    raw_name="ПАО Ромашка",
)


def _run_passport(**overrides):
    base = dict(
        goal_ir_id="goal-1",
        classification=_CLASSIFICATION,
        entity=_ENTITY,
        registry_version="registry-2026.09",
        methodology_version="methodology-1.0",
        model_version="claude-sonnet-5",
        prompt_version="prompt-1.0",
        sandbox_image_version="sandbox-2026.09",
    )
    base.update(overrides)
    return RunPassport(**base)


def test_run_passport_fixes_classification_once():
    passport = _run_passport()
    assert passport.classification == _CLASSIFICATION
    assert passport.classification.profile_id == "ifrs_nf_ru"


@pytest.mark.parametrize(
    "field",
    ["registry_version", "methodology_version", "model_version", "prompt_version", "sandbox_image_version"],
)
def test_run_passport_requires_every_version_field(field):
    with pytest.raises(ValidationError):
        _run_passport(**{field: ""})


def _scalar_fact(**overrides):
    base = dict(
        produced_by_atom="extract.scalar",
        produced_by_atom_version="1.0.0",
        source_ref="doc-1#p12",
        value=123.45,
        unit="RUB_MLN",
        period_ref="p-2025-q1",
    )
    base.update(overrides)
    return ScalarFact(**base)


def test_evidence_carries_producer_and_source():
    fact = _scalar_fact()
    assert fact.produced_by_atom == "extract.scalar"
    assert fact.produced_by_atom_version == "1.0.0"
    assert fact.source_ref == "doc-1#p12"


@pytest.mark.parametrize("field", ["produced_by_atom", "produced_by_atom_version", "source_ref"])
def test_evidence_rejects_anonymous_provenance(field):
    with pytest.raises(ValidationError):
        _scalar_fact(**{field: ""})


def test_correction_produces_a_new_evidence_linked_to_the_original_not_a_mutation():
    original = _scalar_fact()
    corrected = _scalar_fact(value=124.00, supersedes=original.evidence_id)

    assert corrected.evidence_id != original.evidence_id
    assert corrected.supersedes == original.evidence_id
    assert original.value == 123.45, "исходное доказательство не должно меняться"
    assert original.supersedes is None


def test_derived_fact_records_its_inputs_not_just_its_value():
    derived = DerivedFact(
        produced_by_atom="calc.formula_engine",
        produced_by_atom_version="1.0.0",
        source_ref="formula:net_debt_ebitda",
        formula_id="net_debt_ebitda",
        formula_version="1.0.0",
        inputs=["ev-1", "ev-2"],
        value=1.8,
    )
    assert derived.inputs == ["ev-1", "ev-2"]


def test_form_output_keeps_an_unavailable_row_instead_of_dropping_it():
    rows = [
        FormRow(kind="metric", enum="SALES_REVENUE", label="Выручка", value=100.0, available=True),
        FormRow(kind="metric", enum="RWA", label="Активы, взвешенные по риску", value=None, available=False),
    ]
    form = FormOutput(output_id="out-1", run_id="run-1", role="dgr", rows=rows)
    assert len(form.rows) == 2
    unavailable = [r for r in form.rows if not r.available]
    assert len(unavailable) == 1
    assert unavailable[0].value is None


def test_form_row_cannot_claim_a_value_while_marked_unavailable():
    with pytest.raises(ValidationError):
        FormRow(kind="metric", enum="RWA", label="x", value=1.0, available=False)


def test_period_axis_rejects_end_before_start():
    with pytest.raises(ValidationError):
        PeriodAxis(period_id="p-bad", kind="duration", start="2025-03-31", end="2025-01-01")


def test_period_axis_instant_needs_no_start():
    instant = PeriodAxis(period_id="p-instant", kind="instant", end="2025-03-31")
    assert instant.start is None


def test_node_key_rejects_blank_atom_name():
    with pytest.raises(ValidationError):
        NodeKey(atom_name="", atom_version="1.0.0", args_fingerprint="abc")


def test_scalar_fact_rejects_non_finite_value():
    with pytest.raises(ValidationError):
        _scalar_fact(value=float("nan"))
    with pytest.raises(ValidationError):
        _scalar_fact(value=float("inf"))
