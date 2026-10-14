"""`verify.bounds` — критерий готовности атома (16a):

1. Все заранее определённые невозможные значения обнаруживаются в 100%
   тестов жёстких ограничений.
2. Мягкое подозрение создаёт предупреждение, но не удаляет и не переписывает
   факт.
3. Границы берутся из версионированного определения показателя/политики
   (`calculated_meta`), а не из скрытого списка в коде.
4. Отсутствие границы не приводит к выдуманному ограничению.

Сценарии из карточки: граница отсутствует; невозможное значение (ниже/выше
жёсткой границы — тот же класс дефекта, что «процент в неправильном
масштабе», когда ошибка масштаба даёт число вне физически возможного
диапазона); мягкая граница; рассчитанный факт.
"""
from __future__ import annotations

from finengine.contracts import CalculatedMetric, DerivedFact, ExtractedMetric, ScalarFact
from finengine.skills.atoms.verify_bounds import check_bounds, verify_bounds_gate
from finengine.skills.spec.atom_registry import NodeArgs


def test_no_declared_bound_is_not_invented():
    finding = check_bounds(
        concept="UNKNOWN_RATIO", evidence_id="ev-1", period_ref="2025", value=999.0,
        hard_min=None, hard_max=None, soft_min=None, soft_max=None,
    )
    assert finding.status == "pass"


def test_hard_lower_bound_violation_fails():
    finding = check_bounds(
        concept="COST_TO_INCOME_RATIO", evidence_id="ev-1", period_ref="2025", value=-0.5,
        hard_min=0.0, hard_max=1.0, soft_min=None, soft_max=None,
    )
    assert finding.status == "fail"
    assert finding.check_kind == "bounds_hard"


def test_hard_upper_bound_violation_fails():
    """Ошибка масштаба (×100) на доле [0,1] даёт значение вне физически
    возможного диапазона — жёсткая граница обязана его поймать."""
    finding = check_bounds(
        concept="COST_TO_INCOME_RATIO", evidence_id="ev-1", period_ref="2025", value=150.0,
        hard_min=0.0, hard_max=1.0, soft_min=None, soft_max=None,
    )
    assert finding.status == "fail"


def test_soft_bound_violation_warns_but_does_not_fail():
    finding = check_bounds(
        concept="ROA", evidence_id="ev-1", period_ref="2025", value=-0.4,
        hard_min=None, hard_max=None, soft_min=-0.35, soft_max=0.35,
    )
    assert finding.status == "warn"
    assert finding.check_kind == "bounds_soft"


def test_soft_warning_does_not_mutate_or_drop_the_fact(monkeypatch):
    registry = {
        "calculated_meta": {
            "ROA": {"min_value": None, "max_value": None, "soft_min_value": -0.35, "soft_max_value": 0.35},
        },
    }
    monkeypatch.setattr("finengine.skills.atoms.verify_bounds.registry_for_passport", lambda passport: registry)
    fact = DerivedFact(
        produced_by_atom="calc.formula", produced_by_atom_version="1.0.0", source_ref="formula:roa",
        concept="ROA", period_ref="2025", formula_id="ROA", formula_version="1.0.0",
        inputs=["ev-a"], value=-0.4,
    )
    metric = CalculatedMetric(derived_fact=fact, concept="ROA", period_ref="2025")

    class _Ctx:
        def dependencies(self):
            return (metric,)

    class _Passport:
        class _Classification:
            profile_id = "test-cell"
        classification = _Classification()

    ctx = _Ctx()
    ctx.run_passport = _Passport()
    result = verify_bounds_gate(NodeArgs(concept="ROA", period_ref="2025"), ctx)
    finding = next(r for r in result if hasattr(r, "check_kind"))
    passed_metric = next(r for r in result if isinstance(r, CalculatedMetric))
    assert finding.status == "warn"
    assert passed_metric.derived_fact.value == -0.4  # значение не переписано
    assert passed_metric is metric


def test_hard_violation_blocks_the_metric_from_the_gate_output(monkeypatch):
    registry = {"calculated_meta": {"COST_TO_INCOME_RATIO": {"min_value": 0.0, "max_value": 1.0}}}
    monkeypatch.setattr("finengine.skills.atoms.verify_bounds.registry_for_passport", lambda passport: registry)
    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
        concept="COST_TO_INCOME_RATIO", period_ref="2025", value=4.1, unit="доля",
    )
    metric = ExtractedMetric(scalar_fact=fact, concept="COST_TO_INCOME_RATIO", period_ref="2025")

    class _Passport:
        class _Classification:
            profile_id = "test-cell"
        classification = _Classification()

    class _Ctx:
        def dependencies(self):
            return (metric,)
    ctx = _Ctx()
    ctx.run_passport = _Passport()
    result = verify_bounds_gate(NodeArgs(concept="COST_TO_INCOME_RATIO", period_ref="2025"), ctx)
    assert len(result) == 1  # только finding, метрика НЕ прошла гейт
    assert result[0].status == "fail"
