"""`ref.prior` — критерий готовности атома (16b):

1. Для эталонного факта выбирается правильный предыдущий сопоставимый период;
   текущий период не может стать собственным prior.
2. Несопоставимая база (разный `basis`) не используется без нормализации —
   даёт `VerificationFinding(status="not_comparable")`, не `ReferenceValue`.
3. Отсутствие предыдущего периода возвращается явно (пустой список), не
   подменяется угадыванием.
4. Выбранный prior имеет прямую ссылку на доказательство
   (`prior_evidence_id`/`prior_metric_id`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from finengine.contracts import (
    CalculatedMetric,
    ClassificationResult,
    EntityIdentity,
    ExtractedMetric,
    ReferenceValue,
    RunPassport,
    ScalarFact,
    VerificationFinding,
)
from finengine.skills.atoms.ref_prior import ref_prior_executor, ref_prior_requires
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


def _extracted(value: float, period_ref: str, *, basis: str | None = None) -> ExtractedMetric:
    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
        concept="REVENUE", period_ref=period_ref, value=value, unit="млн. руб.", basis=basis,
    )
    return ExtractedMetric(scalar_fact=fact, concept="REVENUE", period_ref=period_ref)


@dataclass
class _Inputs:
    document_text: str = "Выручка за 2025 год составила 1500. Выручка за 2024 год составила 1200."


@dataclass
class _Ctx:
    _deps: list
    run_passport: RunPassport | None
    run_id: str = "run-1"
    inputs: _Inputs = field(default_factory=_Inputs)

    def dependencies(self):
        return tuple(self._deps)


def test_requires_none_without_run_passport_or_concept():
    assert ref_prior_requires(NodeArgs(concept="REVENUE"), None) == ()
    assert ref_prior_requires(NodeArgs(concept=None), _passport()) == ()


def test_prior_period_selected_when_both_periods_present():
    # Периоды текста фикстуры ("Выручка за 2025 год..."/"...за 2024 год...")
    # проходят через ТОТ ЖЕ детектор (`bulk._select_run_periods`), которым
    # реально пользуется `read.value`/`locate` — он канонизирует их в
    # "FY 2025"/"FY 2024", не оставляет голым "2025"/"2024" (см. докстринг
    # `ref_prior.py`: атом не изобретает вторую эвристику периода).
    current = _extracted(1500.0, "2025")
    prior = _extracted(1200.0, "FY 2024")
    ctx = _Ctx(_deps=[current, prior], run_passport=_passport())
    result = ref_prior_executor(NodeArgs(concept="REVENUE", period_ref="2025"), ctx)
    assert len(result) == 1
    ref = result[0]
    assert isinstance(ref, ReferenceValue)
    assert ref.prior_period_ref == "FY 2024"
    assert ref.prior_evidence_id == prior.scalar_fact.evidence_id
    assert ref.prior_metric_id == prior.metric_id
    assert ref.current_evidence_id == current.scalar_fact.evidence_id
    assert ref.basis_status == "comparable"


def test_current_period_never_becomes_its_own_prior():
    current = _extracted(1500.0, "2025")
    ctx = _Ctx(_deps=[current], run_passport=_passport())
    result = ref_prior_executor(NodeArgs(concept="REVENUE", period_ref="2025"), ctx)
    assert result == []


def test_missing_prior_period_returns_explicit_empty_result():
    current = _extracted(1500.0, "2025")
    other_concept = ExtractedMetric(
        scalar_fact=ScalarFact(
            produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-2",
            concept="COST_OF_SALES", period_ref="2024", value=999.0, unit="млн. руб.",
        ),
        concept="COST_OF_SALES", period_ref="2024",
    )
    ctx = _Ctx(_deps=[current, other_concept], run_passport=_passport())
    result = ref_prior_executor(NodeArgs(concept="REVENUE", period_ref="2025"), ctx)
    assert result == []


def test_incompatible_basis_gives_not_comparable_finding_not_reference_value():
    current = _extracted(1500.0, "2025", basis="ifrs")
    prior = _extracted(1200.0, "FY 2024", basis="rsbu")
    ctx = _Ctx(_deps=[current, prior], run_passport=_passport())
    result = ref_prior_executor(NodeArgs(concept="REVENUE", period_ref="2025"), ctx)
    assert len(result) == 1
    assert isinstance(result[0], VerificationFinding)
    assert result[0].status == "not_comparable"


def test_incompatible_unit_gives_not_comparable_finding_not_reference_value():
    current = ExtractedMetric(
        scalar_fact=ScalarFact(
            produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
            concept="REVENUE", period_ref="2025", value=1500.0, unit="млн. руб.",
        ),
        concept="REVENUE", period_ref="2025",
    )
    prior = ExtractedMetric(
        scalar_fact=ScalarFact(
            produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
            concept="REVENUE", period_ref="FY 2024", value=1200.0, unit="тыс. руб.",
        ),
        concept="REVENUE", period_ref="FY 2024",
    )
    ctx = _Ctx(_deps=[current, prior], run_passport=_passport())
    result = ref_prior_executor(NodeArgs(concept="REVENUE", period_ref="2025"), ctx)
    assert len(result) == 1
    assert isinstance(result[0], VerificationFinding)
    assert result[0].status == "not_comparable"
    assert result[0].check_kind == "unit"


def test_calculated_metric_can_serve_as_prior_side():
    from finengine.contracts import DerivedFact

    current = _extracted(1500.0, "2025")
    prior_fact = DerivedFact(
        produced_by_atom="calc.formula", produced_by_atom_version="1.0.0", source_ref="formula:x",
        concept="REVENUE", period_ref="FY 2024", formula_id="X", formula_version="1.0.0",
        inputs=["ev-a"], value=1200.0,
    )
    prior = CalculatedMetric(derived_fact=prior_fact, concept="REVENUE", period_ref="FY 2024")
    ctx = _Ctx(_deps=[current, prior], run_passport=_passport())
    result = ref_prior_executor(NodeArgs(concept="REVENUE", period_ref="2025"), ctx)
    assert len(result) == 1
    assert isinstance(result[0], ReferenceValue)
    assert result[0].prior_evidence_id == prior_fact.evidence_id


def test_raises_without_run_passport():
    current = _extracted(1500.0, "2025")
    ctx = _Ctx(_deps=[current], run_passport=None)
    with pytest.raises(ValueError):
        ref_prior_executor(NodeArgs(concept="REVENUE", period_ref="2025"), ctx)
