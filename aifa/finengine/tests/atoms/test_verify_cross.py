"""`verify.cross` — критерий готовности атома (16a):

1. Все размеченные межфактные противоречия/тождественные проверки
   обнаруживаются на тестовом наборе.
2. Округление в пределах заранее заданного допуска не маркируется как
   конфликт; превышение допуска маркируется.
3. Проверка не исправляет число автоматически и хранит обе конфликтующие
   стороны.
4. Недостаток входов даёт статус «не проверено», а не «проверено успешно».

Краевой случай карточки: сначала проверяется `norm.basis`; несопоставимые
факты нельзя объявлять конфликтующими численно (здесь — упрощённый базис
через `EvidenceRecord.basis`, см. докстринг `verify_identity_cross.py`).
"""
from __future__ import annotations

from finengine.contracts import CalculatedMetric, DerivedFact, ExtractedMetric, ScalarFact
from finengine.skills.atoms.verify_identity_cross import DEFAULT_CROSS_TOLERANCE, verify_cross_gate, verify_cross_requires
from finengine.skills.spec.atom_registry import NodeArgs

_DUAL_NATURE_REGISTRY = {
    "extractable": [{"enum": "EBITDA"}],
    "calculated": [("EBITDA", "EBITDA", "EBIT + DEPRECIATION + AMORTIZATION", [])],
}
_CALCULATED_ONLY_REGISTRY = {
    "extractable": [],
    "calculated": [("GROSS_MARGIN", "Валовая маржа", "(REVENUE - COST_OF_SALES) / REVENUE", [])],
}


class _Passport:
    class _Classification:
        profile_id = "test-cell"

    classification = _Classification()


class _Ctx:
    def __init__(self, dependencies, run_passport=None):
        self._deps = dependencies
        self.run_passport = run_passport

    def dependencies(self):
        return tuple(self._deps)


def _extracted(concept: str, value: float, basis: str | None = None) -> ExtractedMetric:
    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
        concept=concept, period_ref="2025", value=value, unit="млн. руб.", basis=basis,
    )
    return ExtractedMetric(scalar_fact=fact, concept=concept, period_ref="2025")


def _calculated(concept: str, value: float, basis: str | None = None) -> CalculatedMetric:
    fact = DerivedFact(
        produced_by_atom="calc.formula", produced_by_atom_version="1.0.0", source_ref="formula:x",
        concept=concept, period_ref="2025", basis=basis, formula_id=concept, formula_version="1.0.0",
        inputs=["ev-a", "ev-b"], value=value,
    )
    return CalculatedMetric(derived_fact=fact, concept=concept, period_ref="2025")


def test_exact_match_passes():
    extracted, calculated = _extracted("EBITDA", 500.0), _calculated("EBITDA", 500.0)
    ctx = _Ctx([extracted, calculated])
    result = verify_cross_gate(NodeArgs(concept="EBITDA", period_ref="2025"), ctx)
    finding = next(r for r in result if hasattr(r, "check_kind"))
    assert finding.status == "pass"


def test_rounding_within_default_tolerance_is_not_a_conflict():
    extracted = _extracted("EBITDA", 1000.0)
    calculated = _calculated("EBITDA", 1000.0 * (1 + DEFAULT_CROSS_TOLERANCE / 2))
    ctx = _Ctx([extracted, calculated])
    result = verify_cross_gate(NodeArgs(concept="EBITDA", period_ref="2025"), ctx)
    finding = next(r for r in result if hasattr(r, "check_kind"))
    assert finding.status == "pass"


def test_material_divergence_beyond_tolerance_is_flagged():
    extracted = _extracted("EBITDA", 1000.0)
    calculated = _calculated("EBITDA", 1300.0)
    ctx = _Ctx([extracted, calculated])
    result = verify_cross_gate(NodeArgs(concept="EBITDA", period_ref="2025"), ctx)
    finding = next(r for r in result if hasattr(r, "check_kind"))
    metric = next(r for r in result if isinstance(r, CalculatedMetric))
    assert finding.status == "fail"
    assert extracted.scalar_fact.value == 1000.0
    assert calculated.derived_fact.value == 1300.0
    assert metric is calculated  # обе стороны сохранены, ни одна не переписана


def test_missing_second_source_is_not_verified_not_passed_silently():
    calculated = _calculated("EBITDA", 500.0)
    ctx = _Ctx([calculated])
    result = verify_cross_gate(NodeArgs(concept="EBITDA", period_ref="2025"), ctx)
    finding = next(r for r in result if hasattr(r, "check_kind"))
    assert finding.status == "not_verified"


def test_different_basis_is_not_compared_numerically():
    extracted = _extracted("EBITDA", 500.0, basis="ifrs")
    calculated = _calculated("EBITDA", 900.0, basis="rsbu")
    ctx = _Ctx([extracted, calculated])
    result = verify_cross_gate(NodeArgs(concept="EBITDA", period_ref="2025"), ctx)
    finding = next(r for r in result if hasattr(r, "check_kind"))
    assert finding.status == "not_comparable"


def test_dynamic_requires_asks_for_extracted_side_for_dual_nature_concept(monkeypatch):
    monkeypatch.setattr(
        "finengine.skills.atoms.verify_identity_cross.registry_for_passport", lambda passport: _DUAL_NATURE_REGISTRY,
    )
    specs = verify_cross_requires(NodeArgs(concept="EBITDA", period_ref="2025"), _Passport())
    assert len(specs) == 1
    assert specs[0].input_type == "ExtractedMetric"


def test_dynamic_requires_stays_empty_for_calculated_only_concept(monkeypatch):
    """Расчётный-только концепт (нет в `extractable`) — оппортунистической
    стороны заведомо неоткуда взять; `verify_cross_requires` не просит её,
    иначе `_resolve()` планировщика пометил бы требование `Unreachable` и
    уронил бы фичесибилити всего графа ради необязательной сверки."""
    monkeypatch.setattr(
        "finengine.skills.atoms.verify_identity_cross.registry_for_passport", lambda passport: _CALCULATED_ONLY_REGISTRY,
    )
    specs = verify_cross_requires(NodeArgs(concept="GROSS_MARGIN", period_ref="2025"), _Passport())
    assert specs == ()
