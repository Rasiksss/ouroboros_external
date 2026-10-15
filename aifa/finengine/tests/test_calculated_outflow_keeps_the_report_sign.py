"""Расчётная половина показателя обязана отдать число в том же знаке, что извлечённая.

ЦЕНА (прогон владельца по Северстали Q1-2025, 20.08.2026, ячейка ifrs_nf_ru).
CAPEX приехал +43 591 при размеченных −43 591, и то же за Q1 2024. CAPEX заведён
И извлекаемым, И расчётным: нашлась печатная строка `(43 591)` — в таблицу едет
−43 591; не нашлась — считает формула, а признак `formula_default_abs_behavior`
снимает знак с обеих покупок, и сумма модулей выезжает плюсом. Одна и та же
строка таблицы меняла знак в зависимости от того, какая её половина сработала.

ЗАМЕР ПО ВСЕЙ РАЗМЕТКЕ (112 документов, 8 непустых корзин из десяти, 13 769
размеченных период-клеток): правило чинит 130 период-случаев и ломает 3 —
все три амазоновские, где разметка пишет CAPEX величиной без знака. Мера:
расчётная строка, у которой разметка держит эталон, сошлась с точностью 5%.
База `4024859` — 204 из 394, с правилом — 331 из 394.

⚠️ ЗНАК СТАВИТСЯ ОТРИЦАНИЕМ, А НЕ `-abs` (переделано 21.08.2026 по итогам
состязательной проверки). Формула считает МОДУЛЬ нетто-потока; когда модуль сам
отрицателен, поток входящий, и печатать его минусом нельзя. Разбор и замер — в
`_result_is_outflow` и в `test_calculated_outflow_reaches_consumers_as_a_module`.
Там же — сторож на то, что знак экрана НЕ доезжает до формул-потребителей: без
него правка завышала `FREE_CASH_FLOW` на два CAPEX.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (проделана 21.08, каждая мутация покраснела):
  1. `value = -value` → `value` в `_eval_calculated_formula` — падает
     `test_capex_of_two_outflows_stays_an_outflow`;
  2. `added <= abs_applied` → `bool(added & abs_applied)` в `_result_is_outflow`
     — падает `test_one_outflow_addend_does_not_make_the_whole_row_an_outflow`
     (⚠️ до 21.08 здесь стоял `test_net_commission_income_is_not_dragged_below_zero`
     — он на этой мутации остаётся зелёным, шапка называла не тот тест);
  3. `_is_plain_sum_formula` → `return True` — падает
     `test_a_ratio_is_never_read_as_an_outflow`;
  4. подпись `-(...)` → `dated` — падает
     `test_signature_reproduces_the_negative_number`.
"""
from __future__ import annotations

from finengine.skills.bulk_extractor.formula_engine import _eval_calculated_formula

# Признак `formula_default_abs_behavior` в реестре ifrs_nf_ru у всех трёх входов
# CAPEX стоит, у OPERATING_PROFIT — нет (двунаправленная).
CAPEX = "ASSET_PURCHASES + INTANGIBLE_ASSET_PURCHASES - ASSET_SALES_PROCEEDS"
ABS_ENUMS = {
    "ASSET_PURCHASES",
    "INTANGIBLE_ASSET_PURCHASES",
    "ASSET_SALES_PROCEEDS",
    "FEE_AND_COMMISSION_INCOME",
    "FEE_AND_COMMISSION_EXPENSE",
    "DEPRECIATION_AMORTIZATION",
    "TOTAL_DEBT",
    "TOTAL_EQUITY",
}


def _value(formula: str, extracted: dict, trace: dict | None = None) -> float:
    return _eval_calculated_formula(
        formula,
        extracted,
        {},
        None,
        formula_default_abs_enums=ABS_ENUMS,
        trace=trace,
    )


def test_capex_of_two_outflows_stays_an_outflow():
    """Числа Северстали Q1 2025: покупки ОС и НМА в скобках, продаж нет."""
    value = _value(
        CAPEX,
        {"ASSET_PURCHASES": -40000.0, "INTANGIBLE_ASSET_PURCHASES": -3591.0,
         "ASSET_SALES_PROCEEDS": 0.0},
    )
    assert value == -43591.0


def test_a_missing_optional_addend_does_not_veto_the_sign():
    """Компания не раскрыла НМА — слагаемое достроено нулём. Ноль в сумму
    ничего не вносит и права вето на знак не имеет."""
    value = _value(
        CAPEX,
        {"ASSET_PURCHASES": -40000.0, "INTANGIBLE_ASSET_PURCHASES": 0.0,
         "ASSET_SALES_PROCEEDS": 0.0},
    )
    assert value == -40000.0


def test_purchases_printed_positive_stay_positive():
    """Отчёт напечатал покупки без скобок — конвенция отчёта плюсовая, и
    правило её не переворачивает."""
    value = _value(
        CAPEX,
        {"ASSET_PURCHASES": 40000.0, "INTANGIBLE_ASSET_PURCHASES": 3591.0,
         "ASSET_SALES_PROCEEDS": 0.0},
    )
    assert value == 43591.0


def test_net_commission_income_is_not_dragged_below_zero():
    """Слагаемое здесь ДОХОД, вычитаемое — расход. Отрицательный расход не
    делает комиссионный доход оттоком: правило смотрит на слагаемые."""
    value = _value(
        "FEE_AND_COMMISSION_INCOME - FEE_AND_COMMISSION_EXPENSE",
        {"FEE_AND_COMMISSION_INCOME": 900.0, "FEE_AND_COMMISSION_EXPENSE": -300.0},
    )
    assert value == 600.0


def test_one_outflow_addend_does_not_make_the_whole_row_an_outflow():
    """Числа Северстали 19.08: операционная прибыль 28 355, амортизация в ОДДС
    напечатана в скобках. Одного слагаемого со снятым знаком мало — иначе
    EBITDA прибыльного года уехала бы в минус."""
    value = _value(
        "OPERATING_PROFIT + DEPRECIATION_AMORTIZATION",
        {"OPERATING_PROFIT": 28355.0, "DEPRECIATION_AMORTIZATION": -9495.0},
    )
    assert value == 37850.0


def test_a_loss_making_year_does_not_flip_ebitda():
    """У OPERATING_PROFIT признак снятия знака в реестре НЕ стоит — она
    двунаправленная. Значит в `abs_applied` она не попадает никогда, и убыток
    не уводит EBITDA в минус этим правилом."""
    value = _value(
        "OPERATING_PROFIT + DEPRECIATION_AMORTIZATION",
        {"OPERATING_PROFIT": -100.0, "DEPRECIATION_AMORTIZATION": -50.0},
    )
    assert value == -50.0


def test_a_ratio_is_never_read_as_an_outflow():
    """У частного слагаемых нет. Оба имени в отчёте отрицательны, но знак
    результата из них не выводится — иначе долговая нагрузка уехала бы в минус."""
    value = _value(
        "TOTAL_DEBT / TOTAL_EQUITY",
        {"TOTAL_DEBT": -400.0, "TOTAL_EQUITY": -200.0},
    )
    assert value == 2.0


def test_signature_reproduces_the_negative_number():
    """Знак снят с РЕЗУЛЬТАТА, а не со входов, — подпись обязана это показать,
    иначе читатель проверяет клетку по формуле, которая её не даёт (Б5)."""
    trace: dict = {}
    value = _value(
        CAPEX,
        {"ASSET_PURCHASES": -40000.0, "INTANGIBLE_ASSET_PURCHASES": -3591.0,
         "ASSET_SALES_PROCEEDS": 0.0},
        trace,
    )
    assert trace["formula"] == f"-({CAPEX})"
    by_enum = {i["enum"]: i for i in trace["inputs"]}
    restated = -(
        by_enum["ASSET_PURCHASES"]["value"]
        + by_enum["INTANGIBLE_ASSET_PURCHASES"]["value"]
        - by_enum["ASSET_SALES_PROCEEDS"]["value"]
    )
    assert restated == value
