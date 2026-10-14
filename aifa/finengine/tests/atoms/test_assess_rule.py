"""`assess.rule` — критерий готовности атома (docs/build-plan/
19-srez-09-dogovory-normativy-metodiki.md, карточка `assess.rule`):

1. Для всех эталонных числовых правил оператор, запас/превышение и статус
   математического сравнения совпадают с детерминированным эталоном.
2. Атом не генерирует качественные ярлыки «хорошо/плохо/рискованно» — в его
   выходе только формализованное сравнение.
3. Неизвестный порог или несовместимые единицы блокируют сравнение.
4. Все числа результата трассируются к входному факту и порогу.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from finengine.contracts import (
    ClassificationResult,
    EntityIdentity,
    ExtractedMetric,
    ReferenceValue,
    RunPassport,
    RuleComparison,
    ScalarFact,
    VerificationFinding,
)
from finengine.skills.atoms.assess_rule import assess_rule_executor, assess_rule_requires
from finengine.skills.spec.atom_registry import NodeArgs


def _passport() -> RunPassport:
    return RunPassport(
        goal_ir_id="goal-1",
        classification=ClassificationResult(
            profile_id="test-cell", standard="ifrs", company_type="non_financial", language="ru",
            reason="test fixture",
        ),
        entity=EntityIdentity(entity_id="ent-1", canonical_name="Тест", raw_name="Тест"),
        registry_version="registry-7", methodology_version="methodology-1",
        model_version="model-1", prompt_version="prompt-1", sandbox_image_version="sandbox-1",
    )


def _metric(value: float, *, unit="x", basis=None, concept="RATIO_NET_DEBT_EBITDA") -> ExtractedMetric:
    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
        concept=concept, period_ref="FY2025", value=value, unit=unit, basis=basis,
    )
    return ExtractedMetric(scalar_fact=fact, concept=concept, period_ref="FY2025")


def _reference(
    *, kind="norm", operator="<=", value=3.5, unit="x", basis=None, concept="RATIO_NET_DEBT_EBITDA",
) -> ReferenceValue:
    return ReferenceValue(
        concept=concept, source_kind=kind, operator=operator, value=value, unit=unit, basis=basis,
        selection_reason="test fixture",
    )


@dataclass
class _Inputs:
    document_text: str = ""


@dataclass
class _Ctx:
    _deps: list
    run_passport: RunPassport | None
    run_id: str = "run-1"
    inputs: _Inputs = field(default_factory=_Inputs)

    def dependencies(self):
        return tuple(self._deps)


def test_requires_metric_and_both_reference_kinds():
    reqs = assess_rule_requires(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), _passport())
    types = {r.input_type for r in reqs}
    assert types == {"MetricEvidence", "ReferenceValue:plan", "ReferenceValue:norm"}
    assert assess_rule_requires(NodeArgs(concept=None), _passport()) == ()
    assert assess_rule_requires(NodeArgs(concept="X"), None) == ()


@pytest.mark.parametrize("operator,actual,threshold,expected_passed,expected_delta,expected_headroom", [
    ("<=", 3.0, 3.5, True, -0.5, 0.5),
    ("<=", 4.0, 3.5, False, 0.5, -0.5),
    ("<=", 3.5, 3.5, True, 0.0, 0.0),
    ("<", 3.0, 3.5, True, -0.5, 0.5),
    (">=", 1.5, 1.2, True, 0.3, 0.3),
    (">=", 1.0, 1.2, False, -0.2, -0.2),
    (">", 1.5, 1.2, True, 0.3, 0.3),
    ("==", 3.5, 3.5, True, 0.0, 0.0),
    ("==", 3.4, 3.5, False, -0.1, pytest.approx(-0.1)),
    ("!=", 3.4, 3.5, True, -0.1, pytest.approx(-0.1)),
])
def test_rule_math_is_exact_and_mechanical(operator, actual, threshold, expected_passed, expected_delta, expected_headroom):
    metric = _metric(actual)
    reference = _reference(operator=operator, value=threshold)
    ctx = _Ctx(_deps=[metric, reference], run_passport=_passport())
    result = assess_rule_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
    assert len(result) == 1
    rc = result[0]
    assert isinstance(rc, RuleComparison)
    assert rc.operator == operator
    assert rc.passed == expected_passed
    assert rc.actual_value == actual
    assert rc.threshold_value == threshold
    assert rc.delta == pytest.approx(expected_delta)
    assert rc.headroom == pytest.approx(expected_headroom)
    assert rc.relative_delta == pytest.approx(expected_delta / abs(threshold))
    assert rc.actual_evidence_id == metric.scalar_fact.evidence_id
    assert rc.threshold_reference_id == reference.reference_id


def test_no_qualitative_verdict_anywhere_in_the_contract():
    forbidden = {"risk", "риск", "good", "bad", "хорошо", "плохо", "высокий", "низкий"}
    assert not (forbidden & set(RuleComparison.model_fields))


def test_missing_reference_gives_no_comparison_not_a_guess():
    metric = _metric(3.0)
    ctx = _Ctx(_deps=[metric], run_passport=_passport())
    result = assess_rule_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
    assert result == []


def test_missing_metric_gives_no_comparison():
    reference = _reference()
    ctx = _Ctx(_deps=[reference], run_passport=_passport())
    result = assess_rule_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
    assert result == []


def test_incomplete_reference_blocks_with_finding_not_a_fabricated_rule():
    metric = _metric(3.0)
    reference = _reference(operator=None, value=None)
    ctx = _Ctx(_deps=[metric, reference], run_passport=_passport())
    result = assess_rule_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
    assert len(result) == 1
    assert isinstance(result[0], VerificationFinding)
    assert result[0].status == "not_comparable"


def test_currency_unit_mismatch_blocks_comparison():
    metric = _metric(3.0, unit="x")
    reference = _reference(unit="%")
    ctx = _Ctx(_deps=[metric, reference], run_passport=_passport())
    result = assess_rule_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
    assert len(result) == 1
    assert isinstance(result[0], VerificationFinding)
    assert result[0].status == "not_comparable"
    assert result[0].check_kind == "unit"


def test_different_debt_definition_blocks_comparison_via_basis():
    metric = _metric(3.0, basis="net_debt_ifrs16")
    reference = _reference(basis="net_debt_excl_leases")
    ctx = _Ctx(_deps=[metric, reference], run_passport=_passport())
    result = assess_rule_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
    assert len(result) == 1
    assert isinstance(result[0], VerificationFinding)
    assert result[0].status == "not_comparable"
    assert result[0].check_kind == "basis"


def test_multiple_covenants_for_the_same_metric_each_get_their_own_comparison():
    metric = _metric(3.0)
    ref_a = _reference(value=3.5, kind="norm")
    ref_b = _reference(value=2.5, kind="plan")
    ctx = _Ctx(_deps=[metric, ref_a, ref_b], run_passport=_passport())
    result = assess_rule_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
    assert len(result) == 2
    thresholds = {rc.threshold_value for rc in result}
    assert thresholds == {3.5, 2.5}
    passed_by_threshold = {rc.threshold_value: rc.passed for rc in result}
    assert passed_by_threshold[3.5] is True
    assert passed_by_threshold[2.5] is False


def test_raises_without_run_passport():
    ctx = _Ctx(_deps=[], run_passport=None)
    with pytest.raises(ValueError):
        assess_rule_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
