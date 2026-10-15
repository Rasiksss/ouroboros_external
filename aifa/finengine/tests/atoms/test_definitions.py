"""`skills/atoms/definitions.py` — definition resolver (16a, A1.6).

Проверяемые инварианты:
1. `resolve_metric_definition` для известного извлекаемого концепта несёт
   unit_kind/scale_hint/formula_default_abs_behavior/period_type ровно те,
   что в строке реестра.
2. Для расчётного-но-не-извлекаемого концепта семантика берётся из
   `calculated_meta` (не `None` — у любого обрабатываемого концепта есть
   каноническая семантика, пусть и пустая).
3. Ad-hoc спецификация даёт `source="ad_hoc"` и не читает реестр вовсе.
4. `resolve_formula_definition` для известной формулы несёт expression,
   зависимости (отфильтрованные по известным концептам — токен DSL вроде
   `ABS` не становится зависимостью), hard/soft границы из `calculated_meta`.
5. Для показателя без формулы — `None`, не исключение.
6. Ad-hoc формула строит зависимости из `AdHocFormulaSpec.dependencies`,
   тоже отфильтрованные по известным концептам.
7. `version` = `RunPassport.registry_version`, зафиксированный на весь прогон;
   без паспорта — `"unversioned"`, не пусто и не исключение.
"""
from __future__ import annotations

from finengine.contracts import AdHocConceptSpec, AdHocFormulaSpec, ClassificationResult, EntityIdentity, RunPassport
from finengine.skills.atoms.definitions import resolve_formula_definition, resolve_metric_definition

_PASSPORT = RunPassport(
    goal_ir_id="goal-1",
    classification=ClassificationResult(
        profile_id="ifrs_nf_ru", standard="ifrs", company_type="non_financial", language="ru",
        reason="test fixture",
    ),
    entity=EntityIdentity(entity_id="ent-1", canonical_name="Тест", raw_name="Тест"),
    registry_version="registry-42", methodology_version="methodology-1",
    model_version="model-1", prompt_version="prompt-1", sandbox_image_version="sandbox-1",
)

_REGISTRY = {
    "extractable": [
        {"enum": "REVENUE", "unit_kind": "money", "scale_hint": "million", "formula_default_abs_behavior": True,
         "period_type": "duration"},
        {"enum": "COST_OF_SALES", "period_type": "duration"},
    ],
    "calculated": [
        ("GROSS_MARGIN", "Валовая маржа", "(REVENUE - COST_OF_SALES) / REVENUE", ["REVENUE", "COST_OF_SALES"]),
        ("EBITDA_MARGIN", "EBITDA margin", "ABS(EBITDA) / REVENUE", ["EBITDA", "REVENUE"]),
    ],
    "calculated_meta": {
        "GROSS_MARGIN": {"unit_kind": "ratio", "annualize": False,
                          "min_value": 0.0, "max_value": 1.0,
                          "soft_min_value": None, "soft_max_value": None},
    },
}


def test_metric_definition_from_registry_row_carries_its_own_fields():
    d = resolve_metric_definition("REVENUE", _REGISTRY, _PASSPORT)
    assert d.unit_kind == "money"
    assert d.scale_hint == "million"
    assert d.formula_default_abs_behavior is True
    assert d.period_type == "duration"
    assert d.source == "registry"
    assert d.version == "registry-42"


def test_metric_definition_for_calculated_only_concept_is_not_none():
    d = resolve_metric_definition("GROSS_MARGIN", _REGISTRY, _PASSPORT)
    assert d.unit_kind == "ratio"
    assert d.source == "registry"


def test_metric_definition_for_unknown_concept_is_empty_not_invented():
    d = resolve_metric_definition("SOME_UNKNOWN_CONCEPT", _REGISTRY, _PASSPORT)
    assert d.unit_kind == ""
    assert d.scale_hint == ""
    assert d.formula_default_abs_behavior is False


def test_metric_definition_ad_hoc_does_not_read_registry():
    spec = AdHocConceptSpec(term="revenue per employee")
    d = resolve_metric_definition("revenue per employee", _REGISTRY, _PASSPORT, ad_hoc=spec)
    assert d.source == "ad_hoc"
    assert d.unit_kind == ""


def test_formula_definition_carries_expression_and_filtered_dependencies():
    f = resolve_formula_definition("GROSS_MARGIN", _REGISTRY, _PASSPORT)
    assert f is not None
    assert f.expression == "(REVENUE - COST_OF_SALES) / REVENUE"
    assert set(f.dependencies) == {"REVENUE", "COST_OF_SALES"}
    assert f.hard_min == 0.0
    assert f.hard_max == 1.0
    assert f.soft_min is None
    assert f.version == "registry-42"
    assert f.source == "registry"


def test_formula_definition_drops_dsl_tokens_and_unknown_concepts_from_dependencies():
    """`EBITDA_MARGIN` зависит от `EBITDA` (нет в реестре этого фикстура) и
    `REVENUE` (есть) — только известное остаётся зависимостью; `ABS` в
    формуле — DSL-функция, не концепт, и не должна попасть в deps вовсе (её
    и так нет в списке `deps` четвёртого элемента кортежа, но тест защищает
    от будущей регрессии, если кто-то станет парсить `expression` вместо
    готового списка deps)."""
    f = resolve_formula_definition("EBITDA_MARGIN", _REGISTRY, _PASSPORT)
    assert f is not None
    assert f.dependencies == ["REVENUE"]


def test_formula_definition_for_extractable_only_concept_is_none():
    assert resolve_formula_definition("REVENUE", _REGISTRY, _PASSPORT) is None


def test_formula_definition_for_unknown_concept_is_none():
    assert resolve_formula_definition("NOT_A_CONCEPT", _REGISTRY, _PASSPORT) is None


def test_ad_hoc_formula_definition_filters_dependencies_to_known_concepts():
    spec = AdHocFormulaSpec(
        term="выручка на одного клиента", expression="REVENUE / CUSTOMER_COUNT",
        dependencies=["REVENUE", "CUSTOMER_COUNT"],
    )
    f = resolve_formula_definition("выручка на одного клиента", _REGISTRY, _PASSPORT, ad_hoc=spec)
    assert f is not None
    assert f.source == "ad_hoc"
    assert f.formula_id == spec.spec_id
    # CUSTOMER_COUNT не входит в известные концепты фикстура — не домысливаем
    # его достижимость.
    assert f.dependencies == ["REVENUE"]


def test_version_falls_back_to_unversioned_without_a_passport():
    d = resolve_metric_definition("REVENUE", _REGISTRY, None)
    assert d.version == "unversioned"
    f = resolve_formula_definition("GROSS_MARGIN", _REGISTRY, None)
    assert f is not None
    assert f.version == "unversioned"
