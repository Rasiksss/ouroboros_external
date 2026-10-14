"""Подпись расчёта обязана ВОСПРОИЗВОДИТЬСЯ по тому, что видит читатель.

ЦЕНА (Б5, состязательная проверка 20.08.2026). Первая редакция подписи закрыла
пустую клетку, но завела три новых способа соврать — и врала она молча, потому
что каждый вход поодиночке выглядел настоящим:

  • `AVG(X)` вычислитель раскрывает ДО разбора выражения, и показателя из-под
    усреднения в подписи не оставалось вовсе. 69 формул из 359 (ROE, ROA, NIM,
    все оборачиваемости) печатали «Чистая прибыль / AVG(Капитал) · Чистая
    прибыль = 1 000» — второго числа нет, проверить нечем;
  • признак `formula_default_abs_behavior` (556 записей во всех десяти
    ячейках) снимает знак отчёта: строка извлечения показывала −20, подпись
    расчёта +20, а `abs(...)` в формуле не стояло;
  • входы снимаются за ОДИН период, а подпись стоит одна на строку. В широкой
    книге с тремя и более колонками она сходилась только с текущей, и читатель
    соседней колонки видел числа, которые её не дают.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (проделана 20.08, каждая мутация покраснела):
  1. `substitutions=None` в вызове `_replace_avg_calls` → падает
     `test_average_input_is_named_with_its_number`;
  2. `abs_applied=None` в вызове `_formula_env` → падает
     `test_input_with_the_sign_stripped_says_so`;
  3. снять стамп периода в конце `_calculate_metrics` → падает
     `test_every_input_names_the_period_it_was_taken_from`;
  4. `annualization_note` → `f"{12 / months:.2f}"` → падает
     `test_annualization_multiplier_is_written_as_an_exact_fraction`.
"""
from __future__ import annotations

from finengine.skills.bulk_extractor.formula_engine import (
    _calculate_metrics,
    _eval_calculated_formula,
    annualization_factor,
    annualization_note,
)


def _inputs(formula: str, extracted: dict, **kwargs) -> tuple[float, list[dict]]:
    trace: dict = {}
    value = _eval_calculated_formula(
        formula, extracted, {}, kwargs.pop("previous", None), trace=trace, **kwargs
    )
    return value, trace["inputs"]


def test_average_input_is_named_with_its_number():
    """ROE считается по СРЕДНЕМУ капиталу. Среднее знает только вычислитель —
    и обязан назвать его во входах, иначе проверить число нечем."""
    value, inputs = _inputs(
        "NET_INCOME / AVG (TOTAL_EQUITY)",
        {"NET_INCOME": 1000.0, "TOTAL_EQUITY": 6000.0},
        previous={"TOTAL_EQUITY": 5000.0},
    )
    by_enum = {i["enum"]: i for i in inputs}
    assert by_enum["TOTAL_EQUITY"]["transform"] == "avg"
    assert by_enum["TOTAL_EQUITY"]["value"] == 5500.0
    # Главное: по напечатанным входам результат получается ровно тот же.
    assert by_enum["NET_INCOME"]["value"] / by_enum["TOTAL_EQUITY"]["value"] == value


def test_average_over_a_compound_expression_keeps_its_whole_text():
    """`AVG(RIGHT_OF_USE_ASSETS + PPE)` — единственное составное усреднение в
    реестре. Назвать его одним из слагаемых значило бы соврать."""
    _value, inputs = _inputs(
        "REVENUE / AVG (RIGHT_OF_USE_ASSETS + PPE)",
        {"REVENUE": 900.0, "RIGHT_OF_USE_ASSETS": 100.0, "PPE": 300.0},
        previous={"RIGHT_OF_USE_ASSETS": 100.0, "PPE": 200.0},
    )
    averaged = [i for i in inputs if i["transform"] == "avg"]
    assert [i["enum"] for i in averaged] == ["RIGHT_OF_USE_ASSETS + PPE"]
    assert averaged[0]["value"] == 350.0


def test_input_with_the_sign_stripped_says_so():
    """Процентные расходы стоят в отчёте со знаком минус, а в формулу идут по
    модулю. Без пометки подпись расходится со строкой извлечения на два знака."""
    value, inputs = _inputs(
        "EBT + INTEREST_EXPENSE",
        {"EBT": 100.0, "INTEREST_EXPENSE": -20.0},
        formula_default_abs_enums={"INTEREST_EXPENSE"},
    )
    by_enum = {i["enum"]: i for i in inputs}
    assert value == 120.0
    assert by_enum["INTEREST_EXPENSE"]["transform"] == "abs"
    assert by_enum["EBT"]["transform"] == ""


def test_positive_input_of_an_abs_metric_is_not_marked():
    """Пометка обязана стоять там, где число ИЗМЕНИЛИ. Признак у показателя
    есть, но знак и так плюсовой — метить нечего, иначе пометка обесценится."""
    _value, inputs = _inputs(
        "EBT + INTEREST_EXPENSE",
        {"EBT": 100.0, "INTEREST_EXPENSE": 20.0},
        formula_default_abs_enums={"INTEREST_EXPENSE"},
    )
    assert {i["enum"]: i["transform"] for i in inputs} == {"EBT": "", "INTEREST_EXPENSE": ""}


def test_every_input_names_the_period_it_was_taken_from():
    """Подпись одна на строку, а колонок в широкой книге бывает пять. Период
    во входе называет ту единственную, которую подпись воспроизводит."""
    trace: dict = {}
    _calculate_metrics(
        [("EBITDA", "EBITDA", "OPERATING_PROFIT + DEPRECIATION_AMORTIZATION",
          ["OPERATING_PROFIT", "DEPRECIATION_AMORTIZATION"])],
        {"OPERATING_PROFIT": 28355.0, "DEPRECIATION_AMORTIZATION": 9495.0},
        trace=trace,
        period="FY 2024",
    )
    assert trace["EBITDA"]["inputs"], "входов нет — тест ослеп"
    assert {i["period"] for i in trace["EBITDA"]["inputs"]} == {"FY 2024"}


def test_annualization_multiplier_is_written_as_an_exact_fraction():
    """Девять месяцев приводятся к году множителем 12/9 = 1,3333…: напечатай
    его округлением — и напечатанное перестанет давать напечатанный результат."""
    assert annualization_note("9M 2024") == "12/9"
    assert annualization_factor("9M 2024") == 12.0 / 9.0
    assert annualization_note("Q1 2024") == "12/3"
    # Годовой период и балансовая дата не приводятся — писать нечего.
    assert annualization_note("FY 2024") == ""
    assert annualization_note("31.03.2024") == ""
