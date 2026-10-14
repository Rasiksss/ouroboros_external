"""`ref.plan` — критерий готовности атома (docs/build-plan/
19-srez-09-dogovory-normativy-metodiki.md, карточка `ref.plan`):

1. Именованный план/параметр заказчика разрешается по точному идентификатору
   и версии политики.
2. Отсутствующий параметр в 100% отрицательных тестов не заменяется
   «типичным» значением.
3. Параметр другого заказчика/версии политики не применяется.
4. Источник и версия планового значения сохраняются в доказательной цепочке.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from finengine.contracts import ClassificationResult, EntityIdentity, ReferenceValue, RunPassport
from finengine.skills.atoms.ref_plan import ref_plan_executor, ref_plan_requires
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


@dataclass
class _Inputs:
    document_text: str = ""


@dataclass
class _Ctx:
    run_passport: RunPassport | None
    run_id: str = "run-1"
    inputs: _Inputs = field(default_factory=_Inputs)

    def dependencies(self):
        return ()


def test_requires_are_empty_plan_comes_through_extra_not_the_graph():
    assert ref_plan_requires(NodeArgs(concept="REVENUE"), None) == ()
    assert ref_plan_requires(NodeArgs(concept="REVENUE"), _passport()) == ()


def test_missing_plan_entry_returns_explicit_empty_result_not_a_fallback():
    ctx = _Ctx(run_passport=_passport())
    result = ref_plan_executor(NodeArgs(concept="REVENUE", extra={}), ctx)
    assert result == []


def test_plan_value_is_wrapped_with_full_provenance():
    entry = {"value": 2000.0, "unit": "млн. руб.", "period_ref": "FY2025", "version": "plan-v3",
              "source_ref": "customer-plan.xlsx#B12"}
    ctx = _Ctx(run_passport=_passport())
    result = ref_plan_executor(
        NodeArgs(concept="REVENUE", extra={"plan_entry": json.dumps(entry)}), ctx,
    )
    assert len(result) == 1
    ref = result[0]
    assert isinstance(ref, ReferenceValue)
    assert ref.source_kind == "plan"
    assert ref.value == 2000.0
    assert ref.unit == "млн. руб."
    assert ref.period_ref == "FY2025"
    assert ref.source_version == "plan-v3"
    assert ref.source_citation == "customer-plan.xlsx#B12"


def test_plan_without_source_or_version_is_rejected():
    entry = {"value": 2000.0, "unit": "млн. руб."}
    ctx = _Ctx(run_passport=_passport())
    result = ref_plan_executor(
        NodeArgs(concept="REVENUE", extra={"plan_entry": json.dumps(entry)}), ctx,
    )
    assert result == []


def test_plan_without_numeric_value_is_rejected():
    entry = {"unit": "млн. руб.", "version": "plan-v3", "source_ref": "customer-plan.xlsx#B12"}
    ctx = _Ctx(run_passport=_passport())
    result = ref_plan_executor(
        NodeArgs(concept="REVENUE", extra={"plan_entry": json.dumps(entry)}), ctx,
    )
    assert result == []


def test_other_customers_policy_version_is_never_used():
    entry = {
        "value": 2000.0, "version": "plan-v3", "source_ref": "customer-plan.xlsx#B12",
        "policy_version": "customer-A",
    }
    ctx = _Ctx(run_passport=_passport())
    result = ref_plan_executor(
        NodeArgs(concept="REVENUE", policy_version="customer-B", extra={"plan_entry": json.dumps(entry)}), ctx,
    )
    assert result == []


def test_matching_policy_version_is_accepted():
    entry = {
        "value": 2000.0, "version": "plan-v3", "source_ref": "customer-plan.xlsx#B12",
        "policy_version": "customer-A",
    }
    ctx = _Ctx(run_passport=_passport())
    result = ref_plan_executor(
        NodeArgs(concept="REVENUE", policy_version="customer-A", extra={"plan_entry": json.dumps(entry)}), ctx,
    )
    assert len(result) == 1
