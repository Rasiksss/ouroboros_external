"""`verify.identity` — критерий готовности атома (16a):

1. Факты другой сущности/версии/периода/базиса в 100% намеренно конфликтных
   тестов не объединяются как один и тот же факт (структурно гарантировано
   графом — один узел = один `NodeArgs`, см. `norm_identity_cross.py`
   докстринг; здесь проверяется базисная часть этого инварианта — конфликт
   базиса не сравнивается численно).
2. Совместимые дубликаты дедуплицируются без потери ссылок на источники
   (здесь: обе стороны сохраняются в `finding.detail`, ни одна не исчезает).
3. Проверка детерминирована и не вызывает модель (чистая функция).
4. Ошибка идентичности создаёт `VerificationFinding` и не уничтожает исходные
   факты.
"""
from __future__ import annotations

from finengine.contracts import CalculatedMetric, DerivedFact, ExtractedMetric, ScalarFact
from finengine.skills.atoms.verify_identity_cross import verify_identity_gate, verify_identity_requires
from finengine.skills.spec.atom_registry import NodeArgs

_REGISTRY_WITH_RULE = {
    "extractable": [{"enum": "TOTAL_ASSETS"}],
    "calculated": [("TOTAL_ASSETS", "Активы всего", "CURRENT_ASSETS + NON_CURRENT_ASSETS", [])],
    "verification_rules": {"TOTAL_ASSETS": {"tolerance": 0.01, "note": "баланс"}},
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


def test_matching_values_within_tolerance_pass(monkeypatch):
    monkeypatch.setattr(
        "finengine.skills.atoms.verify_identity_cross.registry_for_passport", lambda passport: _REGISTRY_WITH_RULE,
    )
    extracted = _extracted("TOTAL_ASSETS", 1000.0)
    calculated = _calculated("TOTAL_ASSETS", 1000.5)
    ctx = _Ctx([extracted, calculated], run_passport=_Passport())
    result = verify_identity_gate(NodeArgs(concept="TOTAL_ASSETS", period_ref="2025"), ctx)
    finding = next(r for r in result if hasattr(r, "check_kind"))
    assert finding.status == "pass"
    assert finding.check_kind == "identity"


def test_diverging_values_beyond_tolerance_fail_but_keep_both_facts(monkeypatch):
    monkeypatch.setattr(
        "finengine.skills.atoms.verify_identity_cross.registry_for_passport", lambda passport: _REGISTRY_WITH_RULE,
    )
    extracted = _extracted("TOTAL_ASSETS", 1000.0)
    calculated = _calculated("TOTAL_ASSETS", 1200.0)
    ctx = _Ctx([extracted, calculated], run_passport=_Passport())
    result = verify_identity_gate(NodeArgs(concept="TOTAL_ASSETS", period_ref="2025"), ctx)
    finding = next(r for r in result if hasattr(r, "check_kind"))
    metric = next(r for r in result if isinstance(r, CalculatedMetric))
    assert finding.status == "fail"
    assert extracted.scalar_fact.value == 1000.0  # исходный факт не тронут
    assert metric is calculated  # расчётный факт не удалён, а передан дальше


def test_conflicting_basis_blocks_numeric_comparison(monkeypatch):
    monkeypatch.setattr(
        "finengine.skills.atoms.verify_identity_cross.registry_for_passport", lambda passport: _REGISTRY_WITH_RULE,
    )
    extracted = _extracted("TOTAL_ASSETS", 1000.0, basis="group")
    calculated = _calculated("TOTAL_ASSETS", 1000.0, basis="parent")
    ctx = _Ctx([extracted, calculated], run_passport=_Passport())
    result = verify_identity_gate(NodeArgs(concept="TOTAL_ASSETS", period_ref="2025"), ctx)
    finding = next(r for r in result if hasattr(r, "check_kind"))
    assert finding.status == "not_comparable"


def test_missing_extracted_side_is_not_verified_not_a_failure(monkeypatch):
    monkeypatch.setattr(
        "finengine.skills.atoms.verify_identity_cross.registry_for_passport", lambda passport: _REGISTRY_WITH_RULE,
    )
    calculated = _calculated("TOTAL_ASSETS", 1000.0)
    ctx = _Ctx([calculated], run_passport=_Passport())
    result = verify_identity_gate(NodeArgs(concept="TOTAL_ASSETS", period_ref="2025"), ctx)
    finding = next(r for r in result if hasattr(r, "check_kind"))
    assert finding.status == "not_verified"


def test_dynamic_requires_returns_nothing_when_no_rule_is_registered(monkeypatch):
    """`verify_identity_requires` — точка, где решается «есть ли правило для
    этого концепта»: без записи в `verification_rules` вторая сторона не
    запрашивается вовсе, значит `_resolve()` планировщика никогда не увидит
    для неё требование и не рискует пометить его `Unreachable`."""
    empty_registry = {"extractable": [{"enum": "REVENUE"}], "calculated": [], "verification_rules": {}}
    monkeypatch.setattr(
        "finengine.skills.atoms.verify_identity_cross.registry_for_passport", lambda passport: empty_registry,
    )
    specs = verify_identity_requires(NodeArgs(concept="REVENUE", period_ref="2025"), _Passport())
    assert specs == ()


def test_dynamic_requires_asks_for_extracted_side_when_rule_is_registered(monkeypatch):
    monkeypatch.setattr(
        "finengine.skills.atoms.verify_identity_cross.registry_for_passport", lambda passport: _REGISTRY_WITH_RULE,
    )
    specs = verify_identity_requires(NodeArgs(concept="TOTAL_ASSETS", period_ref="2025"), _Passport())
    assert len(specs) == 1
    assert specs[0].input_type == "ExtractedMetric"
    assert specs[0].concept == "TOTAL_ASSETS"


def test_no_calculated_side_at_all_returns_nothing_to_check(monkeypatch):
    monkeypatch.setattr(
        "finengine.skills.atoms.verify_identity_cross.registry_for_passport", lambda passport: _REGISTRY_WITH_RULE,
    )
    ctx = _Ctx([], run_passport=_Passport())
    result = verify_identity_gate(NodeArgs(concept="TOTAL_ASSETS", period_ref="2025"), ctx)
    assert result == []
