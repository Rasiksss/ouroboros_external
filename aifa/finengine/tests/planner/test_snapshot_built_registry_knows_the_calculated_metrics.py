"""Боевой реестр атомов знает расчётные показатели так же, как реестр по профилю.

Боевой путь (`production_runtime`) строит `AtomRegistry` из снимка, замороженного в
паспорте прогона (`build_registry(registry_data=compiled.to_effective_data())`). В
снимке расчётные записи — СЛОВАРИ (`{"enum": ...}`), а `registry_for_profile()` отдаёт
кортежи `(enum, name_ru, formula, deps)`. Читалка `_calc_entry_enum` понимала только
кортежи: в бою `calculated_enums` оказывался пуст, и ни у одного расчётного показателя
не было производителя `calc.formula` — как `MetricEvidence` (вход формулы, роль-набор
полного анализа) он был недостижим. Живая проверка M2 (dev, РСБУ ЛУКОЙЛ):
«недостижимые концепты: TOTAL_LIABILITIES — нет зарегистрированного атома-производителя».

Тесты на `build_registry()` без снимка этого не видели: у них форма записи другая.
"""
from __future__ import annotations

import pytest

from finengine.contracts import ClassificationResult
from finengine.skills.atoms.graph_atoms import build_registry
from finengine.skills.metric_schema import compiled_registry_for_classification

_CELLS = [("rsbu_nf_ru", "rsbu"), ("ifrs_nf_ru", "ifrs")]


def _classification(profile_id: str, standard: str) -> ClassificationResult:
    return ClassificationResult(
        profile_id=profile_id, standard=standard, company_type="non_financial", language="ru", reason="fixture",
    )


def _producers(registry, enum: str) -> list[str]:
    return sorted(atom.name for atom in registry.producers_of("MetricEvidence", enum))


@pytest.mark.parametrize("profile_id, standard", _CELLS)
def test_every_calculated_metric_has_the_same_producers_when_built_from_the_passport_snapshot(profile_id, standard):
    snapshot = compiled_registry_for_classification(_classification(profile_id, standard)).to_effective_data()
    from_snapshot = build_registry(profile_id=profile_id, registry_data=snapshot)
    from_profile = build_registry(profile_id=profile_id)
    calculated = sorted({row["enum"] for row in snapshot["calculated"]})
    assert len(calculated) > 5, "в ячейке есть расчётные показатели — иначе тест ничего не проверяет"

    diverging = {
        enum: (_producers(from_snapshot, enum), _producers(from_profile, enum))
        for enum in calculated
        if _producers(from_snapshot, enum) != _producers(from_profile, enum)
    }

    assert not diverging, f"снимок паспорта и профиль расходятся в производителях: {diverging}"


def test_a_calculated_only_metric_is_produced_by_calc_formula_in_the_production_registry():
    """`TOTAL_LIABILITIES` в РСБУ — расчётный (`TOTAL_ASSETS - TOTAL_EQUITY`), не извлекаемый."""
    snapshot = compiled_registry_for_classification(_classification("rsbu_nf_ru", "rsbu")).to_effective_data()
    registry = build_registry(profile_id="rsbu_nf_ru", registry_data=snapshot)

    assert _producers(registry, "TOTAL_LIABILITIES") == ["calc.formula"]
