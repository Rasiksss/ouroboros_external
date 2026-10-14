"""`norm.time` — критерий готовности атома (16a):

1. Моментные и интервальные показатели приводятся только допустимыми
   правилами; запрещено автоматически превращать квартальный поток в годовой
   без объявленного правила (уже гарантировано схемой `CalcDef` — здесь
   проверяется, что трассировка годизации отражает именно это решение).
2. Период результата и используемое преобразование воспроизводимы в 100%
   детерминированных тестов.
3. Неоднозначный период блокирует нормализацию вместо выбора ближайшего периода.
4. Преобразование не меняет исходное доказательство и сохраняет трассировку.

Сценарии из карточки: 3/6/9 месяцев, 53-недельный год (не годизируется
эвристикой — карточка не даёт справочника недельных лет, поэтому здесь не
изобретается; см. отчёт по атому), моментный показатель, отсутствие прошлого
YTD (LTM/YTD — вне периметра этой сессии, см. докстринг `norm_time.py`),
показатель уже учитывает длину периода (`_one_annualization_not_two`,
существующий тест схемы, не дублируется здесь).
"""
from __future__ import annotations

from finengine.contracts import MetricDefinition, ScalarFact
from finengine.skills.atoms.norm_time import annualization_trace, classify_extracted_period


def _fact(period_ref: str, concept: str = "REVENUE") -> ScalarFact:
    return ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0",
        source_ref="loc-1", concept=concept, period_ref=period_ref, value=100.0, unit="млн. руб.",
    )


def _def(period_type: str, concept: str = "REVENUE") -> MetricDefinition:
    return MetricDefinition(concept=concept, version="registry-1", period_type=period_type)


def test_point_metric_with_duration_period_is_a_conflict():
    fact = _fact("9M", concept="TOTAL_ASSETS")
    _f, finding = classify_extracted_period(fact, _def("point", concept="TOTAL_ASSETS"))
    assert finding.status == "needs_period_resolution"


def test_duration_metric_with_point_date_period_is_a_conflict():
    fact = _fact("31.03.2025")
    _f, finding = classify_extracted_period(fact, _def("duration"))
    assert finding.status == "needs_period_resolution"


def test_duration_metric_with_duration_period_passes():
    fact = _fact("9M")
    normalized, finding = classify_extracted_period(fact, _def("duration"))
    assert normalized is fact
    assert finding.status == "pass"


def test_point_metric_with_point_date_passes():
    fact = _fact("31.03.2025", concept="TOTAL_ASSETS")
    normalized, finding = classify_extracted_period(fact, _def("point", concept="TOTAL_ASSETS"))
    assert normalized is fact
    assert finding.status == "pass"


def test_period_outside_known_run_periods_is_ambiguous_not_guessed():
    fact = _fact("9M")
    _f, finding = classify_extracted_period(fact, _def("duration"), known_periods=frozenset({"FY", "6M"}))
    assert finding.status == "needs_period_resolution"
    assert "9M" in finding.detail


def test_fact_object_is_never_mutated_only_wrapped_in_a_finding():
    fact = _fact("9M", concept="TOTAL_ASSETS")
    returned_fact, _finding = classify_extracted_period(fact, _def("point", concept="TOTAL_ASSETS"))
    assert returned_fact is fact  # блокировка не удаляет и не переписывает факт


def test_annualization_trace_reflects_formula_declared_rule_not_a_new_decision():
    finding = annualization_trace("NET_DEBT_EBITDA", "NET_DEBT / ANNUAL(EBITDA)", "6M")
    assert finding.status == "pass"
    assert "6M" in finding.detail or "ANNUAL" in finding.detail.upper()


def test_annualization_trace_on_full_year_is_a_no_op_multiplier():
    finding = annualization_trace("ROE", "NET_INCOME / AVG(TOTAL_EQUITY)", "12M")
    assert finding.status == "pass"


def test_annualization_trace_is_deterministic_across_repeated_calls():
    a = annualization_trace("NET_DEBT_EBITDA", "NET_DEBT / ANNUAL(EBITDA)", "9M")
    b = annualization_trace("NET_DEBT_EBITDA", "NET_DEBT / ANNUAL(EBITDA)", "9M")
    assert a.detail == b.detail
    assert a.status == b.status
