"""Расчётная строка называет, ПО ЧЕМУ она посчитана: формулу и её входы.

ЦЕНА (Б5, 19.08.2026). До этой правки в строке стоял только ТЕКСТ формулы из
реестра, и проверить число читателю было не по чему. Хуже того — текст в двух
случаях расходился с расчётом молча:

  • отсутствующий необязательный вход вычислитель достраивает нулём, и формула
    на вид оставалась полной: «Общий долг» из четырёх слагаемых выглядел
    посчитанным по четырём, хотя двух в отчёте не было;
  • фолбэк EBITDA считает снизу вверх через EBT, а подписывался реестровой
    формулой `OPERATING_PROFIT + DEPRECIATION_AMORTIZATION`.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (проделана 19.08, каждый покраснел):
  1. убрать `"inputs"` из строки в `ratios.py` → падают все три;
  2. заменить пометку `substituted_zero` на `False` в `_formula_inputs`
     (`formula_engine.py`) → падает `test_missing_optional_input…`;
  3. снять запись происхождения в фолбэке (`formula_engine.py`) — то есть
     вернуть поведение до правки — → падает `test_fallback_ebitda…`, и падает
     он ровно тем, что подпись равна реестровой формуле.
"""
from __future__ import annotations

from finengine.skills.ratios import compute_ratios

PROFILE = "ifrs_nf_ru"
PERIOD = "2024"


def _row(enum: str, value: float) -> dict:
    return {
        "type": "metric",
        "enum": enum,
        "name_ru": enum,
        "value_current": value,
        "value_prev": None,
        "period_values": [{"period": PERIOD, "value": value, "unit": "млн ₽", "page": 7}],
        "unit": "млн ₽",
        "found": True,
        "is_calculated": False,
        "formula": "",
    }


def _by_enum(rows: list[dict], enum: str) -> dict:
    found = [r for r in rows if r.get("enum") == enum]
    assert found, f"строка {enum} не посчиталась: {[r.get('enum') for r in rows]}"
    return found[0]


def test_calculated_row_carries_the_numbers_it_was_computed_from():
    """Северсталь: операционная прибыль 28 355 и амортизация 9 495 — оба числа
    обязаны стоять во входах строки EBITDA, а не только в её формуле."""
    rows = compute_ratios(
        [_row("OPERATING_PROFIT", 28355.0), _row("DEPRECIATION_AMORTIZATION", 9495.0)],
        PROFILE,
        only=["EBITDA"],
        period_current=PERIOD,
    )
    ebitda = _by_enum(rows, "EBITDA")
    assert ebitda["value_current"] == 37850.0
    assert {(i["enum"], i["value"]) for i in ebitda["inputs"]} == {
        ("OPERATING_PROFIT", 28355.0),
        ("DEPRECIATION_AMORTIZATION", 9495.0),
    }


def test_missing_optional_input_is_marked_as_substituted_zero():
    """У общего долга четыре слагаемых; лизинговых обязательств в отчёте нет.
    Вычислитель достраивает их нулём — и во входах это видно."""
    rows = compute_ratios(
        [_row("LONG_TERM_DEBT", 500.0), _row("SHORT_TERM_BORROWINGS", 120.0)],
        PROFILE,
        only=["TOTAL_DEBT"],
        period_current=PERIOD,
    )
    total_debt = _by_enum(rows, "TOTAL_DEBT")
    assert total_debt["value_current"] == 620.0
    zeroed = {i["enum"] for i in total_debt["inputs"] if i["substituted_zero"]}
    assert zeroed == {"LONG_TERM_LEASE_LIABILITIES", "SHORT_TERM_LEASE_LIABILITIES"}
    assert all(i["value"] == 0.0 for i in total_debt["inputs"] if i["substituted_zero"])


def test_fallback_ebitda_signs_its_own_formula_not_the_registry_one():
    """Операционной прибыли нет — считает фолбэк через EBT. Подпись строки
    обязана назвать ФАКТИЧЕСКОЕ выражение, иначе читатель проверяет число по
    формуле, которая его не даёт."""
    rows = compute_ratios(
        [
            _row("EBT", 100.0),
            _row("INTEREST_PAID", -20.0),
            _row("DEPRECIATION_AMORTIZATION", 30.0),
        ],
        PROFILE,
        only=["EBITDA"],
        period_current=PERIOD,
    )
    ebitda = _by_enum(rows, "EBITDA")
    assert ebitda["value_current"] == 150.0
    assert ebitda["formula"] != "OPERATING_PROFIT + DEPRECIATION_AMORTIZATION"
    assert "EBT" in ebitda["formula"]
    assert {i["enum"] for i in ebitda["inputs"]} == {
        "EBT",
        "INTEREST_PAID",
        "DEPRECIATION_AMORTIZATION",
    }
