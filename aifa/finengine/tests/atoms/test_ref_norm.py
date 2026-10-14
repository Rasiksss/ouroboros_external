"""`ref.norm` — критерий готовности атома (docs/build-plan/
19-srez-09-dogovory-normativy-metodiki.md, карточка `ref.norm`):

1. Каждая принятая норма имеет источник, дату/версию, юрисдикцию и
   применимость к сущности/периоду.
2. Норма другой юрисдикции не используется только потому, что документ
   написан на соответствующем языке.
3. Отсутствующий или неподтверждённый норматив не превращается в числовой
   порог.
4. Изменение версии нормы создаёт новое доказательство, а не переписывает
   старое (несколько условий/версий сосуществуют как отдельные записи).
5. Явный приоритет источника: подтверждённое `ConditionEvidence` не
   смешивается с версionированным справочником для того же концепта.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from finengine.contracts import (
    ConditionEvidence,
    EntityIdentity,
    ReferenceValue,
    RunPassport,
    VerificationFinding,
)
from finengine.skills.atoms.ref_norm import ref_norm_executor, ref_norm_requires
from finengine.skills.atoms.run_passport import classification_from_cell
from finengine.skills.spec.atom_registry import NodeArgs


def _passport(cell: str = "ifrs_nf_ru") -> RunPassport:
    return RunPassport(
        goal_ir_id="goal-1",
        classification=classification_from_cell(cell),  # оси — как в бою: standard="ifrs", не id ячейки
        entity=EntityIdentity(entity_id="ent-1", canonical_name="Тест", raw_name="Тест"),
        registry_version="registry-7", methodology_version="methodology-1",
        model_version="model-1", prompt_version="prompt-1", sandbox_image_version="sandbox-1",
    )


def _condition(
    *, concept="RATIO_NET_DEBT_EBITDA", operator="<=", threshold_value=3.5, unit="x",
    period_ref=None, basis=None, evidence_id_suffix="1",
) -> ConditionEvidence:
    return ConditionEvidence(
        produced_by_atom="read.condition", produced_by_atom_version="1.0.0",
        source_ref=f"doc-1@page:5#{evidence_id_suffix}", document_version="v1",
        concept=concept, period_ref=period_ref, basis=basis,
        condition_text="Net Debt to EBITDA shall not exceed 3.5x.", page=5,
        operator=operator, threshold_value=threshold_value, unit=unit,
    )


def _passing_finding(condition: ConditionEvidence) -> VerificationFinding:
    return VerificationFinding(
        produced_by_atom="verify.citation", produced_by_atom_version="1.0.0",
        source_ref=condition.evidence_id, check_kind="citation", status="pass", detail="ok",
    )


def _failing_finding(condition: ConditionEvidence) -> VerificationFinding:
    return VerificationFinding(
        produced_by_atom="verify.citation", produced_by_atom_version="1.0.0",
        source_ref=condition.evidence_id, check_kind="citation", status="fail", detail="not found",
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


def test_requires_condition_evidence_for_the_concept():
    reqs = ref_norm_requires(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), _passport())
    assert len(reqs) == 1
    assert reqs[0].input_type == "ConditionEvidence"
    assert reqs[0].concept == "RATIO_NET_DEBT_EBITDA"
    assert ref_norm_requires(NodeArgs(concept=None), _passport()) == ()
    assert ref_norm_requires(NodeArgs(concept="X"), None) == ()


def test_verified_condition_becomes_reference_value():
    condition = _condition()
    ctx = _Ctx(_deps=[condition, _passing_finding(condition)], run_passport=_passport())
    result = ref_norm_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
    assert len(result) == 1
    ref = result[0]
    assert isinstance(ref, ReferenceValue)
    assert ref.source_kind == "norm"
    assert ref.operator == "<="
    assert ref.value == 3.5
    assert ref.unit == "x"
    assert ref.source_evidence_id == condition.evidence_id


def test_condition_with_no_citation_finding_is_not_trusted():
    condition = _condition()
    ctx = _Ctx(_deps=[condition], run_passport=_passport())  # нет VerificationFinding вовсе
    result = ref_norm_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
    assert result == []


def test_condition_with_failed_citation_is_not_trusted():
    condition = _condition()
    ctx = _Ctx(_deps=[condition, _failing_finding(condition)], run_passport=_passport())
    result = ref_norm_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
    assert result == []


def test_incomplete_condition_is_never_used_as_a_numeric_rule():
    condition = _condition(operator=None, threshold_value=None)
    ctx = _Ctx(_deps=[condition, _passing_finding(condition)], run_passport=_passport())
    result = ref_norm_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
    assert result == []


def test_missing_norm_returns_explicit_empty_result_not_a_fallback():
    ctx = _Ctx(_deps=[], run_passport=_passport())
    result = ref_norm_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
    assert result == []


def test_multiple_covenants_for_the_same_metric_all_survive():
    c1 = _condition(threshold_value=3.5, evidence_id_suffix="1")
    c2 = _condition(threshold_value=2.5, evidence_id_suffix="2")
    ctx = _Ctx(_deps=[c1, c2, _passing_finding(c1), _passing_finding(c2)], run_passport=_passport())
    result = ref_norm_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
    assert len(result) == 2
    assert {r.value for r in result} == {3.5, 2.5}
    assert {r.source_evidence_id for r in result} == {c1.evidence_id, c2.evidence_id}


def test_threshold_applicable_only_to_its_own_period_is_selected():
    old = _condition(threshold_value=3.5, period_ref="FY2024", evidence_id_suffix="old")
    new = _condition(threshold_value=3.0, period_ref="FY2025", evidence_id_suffix="new")
    ctx = _Ctx(_deps=[old, new, _passing_finding(old), _passing_finding(new)], run_passport=_passport())
    result = ref_norm_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA", period_ref="FY2025"), ctx)
    assert len(result) == 1
    assert result[0].value == 3.0


def test_registry_norm_used_only_when_no_condition_available():
    registry = {"RATIO_NET_DEBT_EBITDA": [{
        "operator": "<=", "value": 4.0, "unit": "x", "version": "policy-v1",
    }]}
    ctx = _Ctx(_deps=[], run_passport=_passport())
    result = ref_norm_executor(
        NodeArgs(concept="RATIO_NET_DEBT_EBITDA", extra={"norm_registry": json.dumps(registry)}), ctx,
    )
    assert len(result) == 1
    assert result[0].source_kind == "norm"
    assert result[0].value == 4.0
    assert result[0].source_version == "policy-v1"


def test_verified_condition_takes_priority_over_registry_not_mixed():
    condition = _condition(threshold_value=3.5)
    registry = {"RATIO_NET_DEBT_EBITDA": [{"operator": "<=", "value": 4.0, "version": "policy-v1"}]}
    ctx = _Ctx(_deps=[condition, _passing_finding(condition)], run_passport=_passport())
    result = ref_norm_executor(
        NodeArgs(concept="RATIO_NET_DEBT_EBITDA", extra={"norm_registry": json.dumps(registry)}), ctx,
    )
    assert len(result) == 1
    assert result[0].value == 3.5  # из ConditionEvidence, не из справочника
    assert result[0].source_evidence_id == condition.evidence_id


def test_other_customers_policy_version_never_used():
    registry = {"RATIO_NET_DEBT_EBITDA": [{
        "operator": "<=", "value": 4.0, "version": "policy-v1", "policy_version": "customer-A",
    }]}
    ctx = _Ctx(_deps=[], run_passport=_passport())
    result = ref_norm_executor(
        NodeArgs(
            concept="RATIO_NET_DEBT_EBITDA", policy_version="customer-B",
            extra={"norm_registry": json.dumps(registry)},
        ),
        ctx,
    )
    assert result == []


def test_norm_of_another_jurisdiction_is_not_used():
    # RunPassport ячейки "ifrs_nf_ru" даёт юрисдикцию "RU" (jurisdiction_of).
    registry = {"RATIO_NET_DEBT_EBITDA": [{
        "operator": "<=", "value": 4.0, "version": "policy-v1", "jurisdiction": "US",
    }]}
    ctx = _Ctx(_deps=[], run_passport=_passport("ifrs_nf_ru"))
    result = ref_norm_executor(
        NodeArgs(concept="RATIO_NET_DEBT_EBITDA", extra={"norm_registry": json.dumps(registry)}), ctx,
    )
    assert result == []


def test_registry_entry_without_version_is_rejected():
    registry = {"RATIO_NET_DEBT_EBITDA": [{"operator": "<=", "value": 4.0}]}
    ctx = _Ctx(_deps=[], run_passport=_passport())
    result = ref_norm_executor(
        NodeArgs(concept="RATIO_NET_DEBT_EBITDA", extra={"norm_registry": json.dumps(registry)}), ctx,
    )
    assert result == []


def test_raises_without_run_passport():
    ctx = _Ctx(_deps=[], run_passport=None)
    import pytest

    with pytest.raises(ValueError):
        ref_norm_executor(NodeArgs(concept="RATIO_NET_DEBT_EBITDA"), ctx)
