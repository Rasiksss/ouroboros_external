"""`norm.unit` — критерий готовности атома (docs/build-plan/
16a-srez-06a-normalizaciya-proverki-raschety.md, карточка `norm.unit`):

1. Все поддерживаемые преобразования масштаба/валютного представления/единицы
   проходят точные табличные тесты без вызова модели.
2. Повторная нормализация уже нормализованного факта идемпотентна.
3. Неизвестная единица не конвертируется предположением; возвращается явная
   невозможность нормализации.
4. Результат хранит исходную и целевую единицу и коэффициент преобразования.

Минимальный набор тестовых сценариев из карточки: тысячи/миллионы, проценты,
базисные пункты, отрицательные затраты, отсутствующая валюта, конфликт единицы.
"""
from __future__ import annotations

from finengine.contracts import MetricDefinition, ScalarFact
from finengine.skills.atoms.norm_unit import normalize_unit


def _fact(value: float, unit: str, concept: str = "REVENUE") -> ScalarFact:
    return ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0",
        source_ref="loc-1", concept=concept, period_ref="2025", value=value, unit=unit,
    )


def _def(unit_kind: str = "", concept: str = "REVENUE") -> MetricDefinition:
    return MetricDefinition(concept=concept, version="registry-1", unit_kind=unit_kind)


def test_thousands_scale_to_canonical_million():
    fact = _fact(1500.0, "тыс. руб.")
    normalized, finding = normalize_unit(fact, _def("money"))
    assert normalized.value == 1.5
    assert normalized.unit == "млн. руб."
    assert normalized.supersedes == fact.evidence_id
    assert finding.status == "pass"
    assert "0.001" in finding.detail or "1000" in finding.detail  # коэффициент присутствует явно


def test_billions_scale_to_canonical_million():
    fact = _fact(2.0, "млрд руб.")
    normalized, _finding = normalize_unit(fact, _def("money"))
    assert normalized.value == 2000.0
    assert normalized.unit == "млн. руб."


def test_negative_outflow_value_keeps_sign_through_scale_conversion():
    fact = _fact(-500.0, "тыс. $", concept="CAPEX")
    normalized, finding = normalize_unit(fact, _def("money", concept="CAPEX"))
    assert normalized.value == -0.5
    assert finding.status == "pass"


def test_percent_unit_is_recognized_and_passthrough():
    fact = _fact(15.7, "%")
    normalized, finding = normalize_unit(fact, _def("percent"))
    assert normalized is fact  # идемпотентно: преобразование не требовалось
    assert finding.status == "pass"


def test_basis_points_are_recognized_not_treated_as_unknown():
    fact = _fact(25.0, "б.п.")
    normalized, finding = normalize_unit(fact, _def("percent"))
    assert normalized is fact
    assert finding.status == "pass"


def test_money_scale_word_without_explicit_currency_is_not_blocked():
    """«Отсутствующая валюта» — заголовок таблицы говорит только «млн», без
    названия валюты. Масштаб уже канонический (1.0), а не домысленная валюта
    — факт проходит, а не блокируется."""
    fact = _fact(42.0, "млн")
    normalized, finding = normalize_unit(fact, _def("money"))
    assert normalized is fact
    assert finding.status == "pass"


def test_unknown_unit_is_not_guessed_and_blocks_normalization():
    fact = _fact(10.0, "штук", concept="HEADCOUNT")
    normalized, finding = normalize_unit(fact, _def("", concept="HEADCOUNT"))
    assert normalized is fact  # факт не мутируется, не удаляется
    assert finding.status == "needs_unit_resolution"


def test_registry_expects_money_but_fact_is_percent_is_a_conflict():
    fact = _fact(5.0, "%", concept="EBITDA")
    _normalized, finding = normalize_unit(fact, _def("money", concept="EBITDA"))
    assert finding.status == "needs_unit_resolution"


def test_registry_expects_ratio_but_fact_carries_money_unit_is_a_conflict():
    fact = _fact(5.0, "млн. руб.", concept="CURRENT_RATIO")
    _normalized, finding = normalize_unit(fact, _def("ratio", concept="CURRENT_RATIO"))
    assert finding.status == "needs_unit_resolution"


def test_renormalizing_an_already_canonical_fact_is_idempotent():
    fact = _fact(1500.0, "тыс. руб.")
    normalized_once, _f1 = normalize_unit(fact, _def("money"))
    normalized_twice, finding_twice = normalize_unit(normalized_once, _def("money"))
    assert normalized_twice is normalized_once  # второй проход не создаёт новый evidence_id
    assert finding_twice.status == "pass"


def test_finding_carries_source_and_target_unit_and_factor():
    fact = _fact(1500.0, "тыс. руб.")
    _normalized, finding = normalize_unit(fact, _def("money"))
    assert "тыс. руб." in finding.detail
    assert "млн. руб." in finding.detail
    assert "0.001" in finding.detail
