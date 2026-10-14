"""Срез 31 (П4/П5): детерминированный слой ad-hoc понятий.

Модель придумывает имя/синонимы/определение (П5) и формулу с компонентами (П4);
код НЕ доверяет ей вслепую: код реестра проверяется по реальным реестрам, символы
формулы обязаны совпадать с описанными компонентами, выражение проходит whitelist.
Здесь — только чистые функции, без модели и графа.
"""
from __future__ import annotations

import pytest

from finengine.contracts import AdHocConceptSpec, AdHocFormulaSpec
from finengine.skills.atoms.ad_hoc_concepts import (
    all_registry_enums,
    concept_spec_from_payload,
    dump_specs,
    formula_rejection_reason,
    formula_spec_from_payload,
    load_specs,
    metric_row,
    registry_code_for,
)


# ---------------------------------------------------------------------------
# П5: строка метрики для двухстадийного экстрактора
def test_metric_row_carries_what_the_model_invented_not_just_the_phrase():
    spec = AdHocConceptSpec(
        term="средневзвешенная ставка по кредитам", key="AVG_LOAN_RATE",
        name_ru="Средневзвешенная процентная ставка по кредитам и займам",
        name_en="Weighted average interest rate on loans",
        aliases_ru=["средняя ставка по кредитам", "эффективная ставка по займам"],
        aliases_en=["weighted average rate"],
        definition="Средняя ставка по долговым обязательствам, раскрываемая в примечании о займах.",
        hint="Ищи в примечании о кредитах и займах, обычно в процентах годовых.",
        value_kind="ratio",
    )
    row = metric_row(spec)
    assert row["enum"] == "AVG_LOAN_RATE"
    assert row["ad_hoc"] is True
    assert row["name_ru"] == "Средневзвешенная процентная ставка по кредитам и займам"
    assert row["name_en"] == "Weighted average interest rate on loans"
    assert row["aliases_ru"] == ["средняя ставка по кредитам", "эффективная ставка по займам"]
    assert row["aliases_en"] == ["weighted average rate"]
    assert "примечании о займах" in row["description"]
    assert "процентах годовых" in row["hint"]


def test_metric_row_falls_back_to_the_term_when_the_model_gave_no_names():
    row = metric_row(AdHocConceptSpec(term="мультипликатор счастья"))
    assert row["enum"] == "мультипликатор счастья"
    assert row["name_ru"] == "мультипликатор счастья"
    assert row["aliases_ru"] == [] and row["aliases_en"] == []


@pytest.mark.parametrize("value_kind, period_type", [("flow", "duration"), ("stock", "point")])
def test_value_kind_is_translated_to_the_extractor_period_type(value_kind, period_type):
    row = metric_row(AdHocConceptSpec(term="x", value_kind=value_kind))
    assert row["period_type"] == period_type


def test_specs_survive_the_node_extra_round_trip():
    specs = [
        AdHocConceptSpec(term="a", key="A_KEY", name_ru="А", aliases_ru=["а1"]),
        AdHocConceptSpec(term="b", key="B_KEY", name_ru="Б"),
    ]
    restored = load_specs({"ad_hoc_concepts": dump_specs(specs)})
    assert set(restored) == {"A_KEY", "B_KEY"}
    assert restored["A_KEY"].aliases_ru == ["а1"]
    assert load_specs({}) == {} and load_specs({"ad_hoc_concepts": "not json"}) == {}


# ---------------------------------------------------------------------------
# П5: понятие из ответа модели
def test_concept_payload_is_normalized_and_limits_alias_noise():
    spec = concept_spec_from_payload(
        {
            "name_ru": "  Средняя ставка ", "name_en": "Average rate",
            "aliases_ru": ["ставка", "ставка", "", "  средняя ставка "], "aliases_en": ["avg rate"],
            "definition": "Определение", "hint": "Подсказка", "value_kind": "ratio",
        },
        fallback_term="какая-то ставка",
    )
    assert spec is not None
    assert spec.name_ru == "Средняя ставка"
    assert spec.aliases_ru == ["ставка", "средняя ставка"], "дубли и пустые синонимы отбрасываются"
    assert spec.term == "Средняя ставка"


def test_concept_payload_without_any_name_uses_the_users_phrase():
    spec = concept_spec_from_payload({"aliases_ru": ["а"]}, fallback_term="ставка по займам")
    assert spec is not None and spec.term == "ставка по займам"


def test_unknown_value_kind_degrades_to_other_not_to_an_error():
    spec = concept_spec_from_payload({"name_ru": "x", "value_kind": "banana"}, fallback_term="x")
    assert spec is not None and spec.value_kind == "other"


def test_non_object_payload_is_rejected():
    assert concept_spec_from_payload("строка", fallback_term="x") is None
    assert concept_spec_from_payload(None, fallback_term="x") is None


# ---------------------------------------------------------------------------
# Реестр как точный индекс для сопоставления компонентов
def test_registry_universe_contains_known_enums_and_finds_exact_names():
    assert "SALES_REVENUE" in all_registry_enums()
    assert registry_code_for(["Выручка"]) == "SALES_REVENUE"
    assert registry_code_for(["выручка от продаж", "мусор"]) in {"SALES_REVENUE", None}


def test_a_label_matching_nothing_gives_no_code():
    assert registry_code_for(["совершенно выдуманное понятие про единорогов"]) is None
    assert registry_code_for([]) is None and registry_code_for([""]) is None


# ---------------------------------------------------------------------------
# П4: формула, которую выдала модель
def _component(key, **kw):
    return {"key": key, "name_ru": kw.pop("name_ru", key), **kw}


def test_formula_from_model_builds_components_and_dependencies():
    spec = formula_spec_from_payload(
        {
            "name_ru": "Чистый долг к EBITDA", "unit": "x",
            "expression": "(TOTAL_DEBT - CASH_EQ) / EBITDA_ADJ",
            # Имена намеренно вымышленные: точное имя реестра сопоставилось бы с
            # кодом реестра (это проверяется отдельным тестом ниже).
            "components": [
                _component("TOTAL_DEBT", name_ru="Долг-икс тестовый", aliases_ru=["кредитный пакет-икс"]),
                _component("CASH_EQ", name_ru="Касса-игрек тестовая"),
                _component("EBITDA_ADJ", name_ru="Показатель-зет тестовый"),
            ],
            "assumptions": "Долг включает краткосрочную часть",
        },
        fallback_term="чистый долг / EBITDA",
    )
    assert isinstance(spec, AdHocFormulaSpec)
    assert spec.expression == "(TOTAL_DEBT - CASH_EQ) / EBITDA_ADJ"
    assert set(spec.dependencies) == {"TOTAL_DEBT", "CASH_EQ", "EBITDA_ADJ"}
    assert {c.concept_key for c in spec.components} == set(spec.dependencies)
    assert spec.assumptions == "Долг включает краткосрочную часть"
    assert spec.unit == "x" and spec.name_ru == "Чистый долг к EBITDA"


def test_a_valid_registry_code_replaces_the_symbol_and_is_rewritten_in_the_expression():
    spec = formula_spec_from_payload(
        {
            "expression": "REV - COGS",
            "components": [
                _component("REV", registry_code="SALES_REVENUE", name_ru="Выручка"),
                _component("COGS", name_ru="Себестоимость продукции придуманная"),
            ],
        },
        fallback_term="валовая",
    )
    assert spec is not None
    assert "SALES_REVENUE" in spec.dependencies, "символ заменён кодом реестра"
    assert spec.expression.startswith("SALES_REVENUE -"), spec.expression
    assert "REV " not in spec.expression.replace("SALES_REVENUE", "")


def test_an_invented_registry_code_is_not_trusted_the_component_stays_ad_hoc():
    spec = formula_spec_from_payload(
        {
            "expression": "A / B",
            "components": [
                _component("A", registry_code="TOTALLY_MADE_UP_CODE", name_ru="Показатель А странный"),
                _component("B", name_ru="Показатель Б странный"),
            ],
        },
        fallback_term="a/b",
    )
    assert spec is not None
    assert set(spec.dependencies) == {"A", "B"}, "выдуманный код не превращается в зависимость реестра"


def test_a_component_is_mapped_to_the_registry_by_exact_name_when_the_model_gave_no_code():
    spec = formula_spec_from_payload(
        {
            "expression": "R / X",
            "components": [
                _component("R", name_ru="Выручка"),
                _component("X", name_ru="Показатель Икс странный"),
            ],
        },
        fallback_term="r/x",
    )
    assert spec is not None
    assert "SALES_REVENUE" in spec.dependencies


def test_standard_quick_ratio_labels_are_rewritten_to_rsbu_registry_codes():
    spec = formula_spec_from_payload(
        {
            "name_ru": "Промежуточный коэффициент покрытия",
            "expression": "CURRENT_ASSETS - INVENTORIES / CURRENT_LIABILITIES",
            "components": {
                "CURRENT_ASSETS": "Оборотные активы",
                "INVENTORIES": "Запасы",
                "CURRENT_LIABILITIES": "Краткосрочные обязательства",
            },
        },
        fallback_term="быстрая ликвидность",
    )

    assert spec is not None
    assert "TOTAL_CURRENT_ASSETS" in spec.dependencies
    assert "TOTAL_CURRENT_LIABILITIES" in spec.dependencies
    assert "CURRENT_ASSETS" not in spec.dependencies
    assert "CURRENT_LIABILITIES" not in spec.dependencies


@pytest.mark.parametrize("expression", [
    "__import__('os')", "A.real / B", "A ** 2", "A // B", "A if B else 1", "max(A, B)", "A; B", "A / (B", "",
])
def test_unsafe_or_malformed_expressions_are_rejected(expression):
    payload = {"expression": expression, "components": [_component("A"), _component("B")]}
    assert formula_spec_from_payload(payload, fallback_term="t") is None


def test_a_symbol_without_a_component_is_rejected():
    payload = {"expression": "A / B", "components": [_component("A")]}
    assert formula_spec_from_payload(payload, fallback_term="t") is None


def test_a_component_not_used_in_the_expression_is_rejected():
    payload = {"expression": "A / B", "components": [_component("A"), _component("B"), _component("C")]}
    assert formula_spec_from_payload(payload, fallback_term="t") is None


def test_lowercase_symbols_are_normalized_consistently():
    spec = formula_spec_from_payload(
        {"expression": "debt / equity",
         "components": [_component("debt", name_ru="Долг мой"), _component("equity", name_ru="Капитал мой")]},
        fallback_term="d/e",
    )
    assert spec is not None
    assert spec.expression == "DEBT / EQUITY" and set(spec.dependencies) == {"DEBT", "EQUITY"}


def test_a_non_ascii_key_is_rejected():
    payload = {"expression": "ДОЛГ / КАПИТАЛ", "components": [_component("ДОЛГ"), _component("КАПИТАЛ")]}
    assert formula_spec_from_payload(payload, fallback_term="t") is None


# ---------------------------------------------------------------------------
# Причина отказа — то, что возвращается модели для одной повторной попытки.
def test_a_valid_formula_has_no_rejection_reason():
    payload = {"expression": "A / B", "components": [_component("A", name_ru="Икс тестовый"), _component("B", name_ru="Игрек тестовый")]}
    assert formula_rejection_reason(payload) == ""


@pytest.mark.parametrize("payload, fragment", [
    ("не объект", "объект"),
    ({"expression": "", "components": []}, "expression"),
    ({"expression": "A / B", "components": None}, "components"),
    ({"expression": "A / B", "components": [None, None]}, "компонент 1"),
    ({"expression": "A / B", "components": [_component("A"), _component("б")]}, "key"),
    ({"expression": "A / B", "components": [_component("A")]}, "символ"),
    ({"expression": "A / B", "components": [_component("A"), _component("B"), _component("C")]}, "символ"),
])
def test_every_rejection_names_its_reason(payload, fragment):
    assert fragment in formula_rejection_reason(payload)


# ---------------------------------------------------------------------------
# Плоская форма компонентов: «символ → название» (устойчивее вложенного массива:
# реальная модель разваливала массив объектов — живая проверка M2, 24.09.2026).
def test_components_may_be_a_flat_map_symbol_to_name():
    spec = formula_spec_from_payload(
        {"name_ru": "Доля заёмных средств", "expression": "DEBT / ASSETS * 100",
         "components": {"DEBT": "Заёмные средства придуманные", "ASSETS": "Итого активов"}},
        fallback_term="доля",
    )
    assert spec is not None
    assert len(spec.dependencies) == 2 and "DEBT" in spec.dependencies
    by_key = {c.concept_key: c for c in spec.components}
    assert by_key["DEBT"].name_ru == "Заёмные средства придуманные"


def test_a_map_value_may_be_a_full_object_too():
    spec = formula_spec_from_payload(
        {"expression": "X / Y", "components": {
            "X": {"name_ru": "Икс тестовый", "aliases_ru": ["икс"], "definition": "определение икс"},
            "Y": "Игрек тестовый",
        }},
        fallback_term="x/y",
    )
    assert spec is not None
    by_key = {c.concept_key: c for c in spec.components}
    assert by_key["X"].aliases_ru == ["икс"] and by_key["X"].definition == "определение икс"
    assert by_key["Y"].aliases_ru == []


@pytest.mark.parametrize("components", [{"X": 5, "Y": "игрек тестовый"}, {"X": None}, {"": "пустой символ"}])
def test_a_map_with_junk_values_is_rejected_with_a_reason(components):
    payload = {"expression": "X / Y", "components": components}
    assert formula_spec_from_payload(payload, fallback_term="t") is None
    assert formula_rejection_reason(payload)
