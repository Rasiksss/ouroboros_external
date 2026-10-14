"""Подпись расчётной СТРОКИ называет колонку, которую она воспроизводит.

ЦЕНА (Б5, состязательная проверка 20.08.2026). Проверяющий прогнал широкую
ролевую книгу и получил строку

    ('EBITDA','млн ₽', 28000, 37850, '= OPERATING_PROFIT + …; входы: … 28355')

Входы сходятся ТОЛЬКО с колонкой 2025. Читатель, сверявший 2024, получал
37 850 вместо 28 000 и решал, что расчёт врёт. До правки клетка «Источник»
была пуста — соврать было нечем; подпись без периода это ухудшила.

ВТОРОЕ (аннуализация, 40 записей реестра). Приведение к году делается ПРИ
ВЫДАЧЕ, после расчёта: ROE за девять месяцев печатается умноженным на 12/9, а
входы сняты за девять месяцев. Арифметика «входы → напечатанное» не сходилась
ровно на этот множитель — и это тот же дефект, а не вопрос вкуса: подпись
обязана совпасть с расчётом. Множитель пишем дробью: 12/9 — это 1,3333…, и
округление не даёт напечатанного результата.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (проделана 20.08):
  1. снять `period_current=period_current` у `_calculate_metrics_pair` в
     `ratios.py` → падает `test_inputs_name_the_period_they_were_taken_from`;
  2. вернуть `"formula": formula_shown` без множителя → падает
     `test_annualized_row_signs_the_multiplier_it_was_scaled_by`.
"""
from __future__ import annotations

from finengine.skills.ratios import compute_ratios

PROFILE = "ifrs_nf_ru"


def _row(enum: str, by_period: dict[str, float]) -> dict:
    periods = list(by_period)
    return {
        "type": "metric",
        "enum": enum,
        "name_ru": enum,
        "value_current": by_period[periods[0]],
        "value_prev": by_period[periods[1]] if len(periods) > 1 else None,
        "period_values": [
            {"period": p, "value": v, "unit": "млн ₽", "page": 7} for p, v in by_period.items()
        ],
        "unit": "млн ₽",
        "found": True,
        "is_calculated": False,
        "formula": "",
    }


def _by_enum(rows: list[dict], enum: str) -> dict:
    found = [r for r in rows if r.get("enum") == enum]
    assert found, f"строка {enum} не посчиталась: {[r.get('enum') for r in rows]}"
    return found[0]


def test_inputs_name_the_period_they_were_taken_from():
    """Три колонки в таблице, подпись одна. Она обязана сказать, какая её."""
    rows = compute_ratios(
        [
            _row("OPERATING_PROFIT", {"2025": 28355.0, "2024": 21000.0, "2023": 17000.0}),
            _row("DEPRECIATION_AMORTIZATION", {"2025": 9495.0, "2024": 7000.0, "2023": 6000.0}),
        ],
        PROFILE,
        only=["EBITDA"],
        period_current="2025",
    )
    ebitda = _by_enum(rows, "EBITDA")
    assert ebitda["inputs"], "входов нет — тест ослеп"
    assert {i["period"] for i in ebitda["inputs"]} == {"2025"}
    assert ebitda["value_current"] == sum(i["value"] for i in ebitda["inputs"])


def test_annualized_row_signs_the_multiplier_it_was_scaled_by():
    """ROE за девять месяцев печатается годовой. Без множителя в подписи
    читатель делит прибыль на средний капитал и не получает напечатанное."""
    rows = compute_ratios(
        [
            _row("NET_INCOME", {"9M 2025": 900.0, "9M 2024": 600.0}),
            _row("TOTAL_EQUITY", {"9M 2025": 6000.0, "9M 2024": 5000.0}),
        ],
        PROFILE,
        only=["ROE"],
        period_current="9M 2025",
        period_prev="9M 2024",
    )
    roe = _by_enum(rows, "ROE")
    assert "* 12/9" in roe["formula"]
    # Сотня тоже обязана быть напечатана: ROE считается В ПРОЦЕНТАХ (решение
    # владельца 20.08), и без неё читатель получит число в сто раз меньше.
    assert "* 100" in roe["formula"] and roe["unit"] == "%"
    by_enum = {i["enum"]: i["value"] for i in roe["inputs"]}
    # По напечатанным входам и напечатанной формуле получается напечатанное.
    assert roe["value_current"] == (
        by_enum["NET_INCOME"] / by_enum["TOTAL_EQUITY"] * 100 * (12 / 9)
    )


def test_average_input_of_a_ratio_row_reaches_the_row():
    """ROE считается по СРЕДНЕМУ капиталу — 5 500, а не 6 000 текущего года."""
    rows = compute_ratios(
        [
            _row("NET_INCOME", {"2025": 1000.0, "2024": 800.0}),
            _row("TOTAL_EQUITY", {"2025": 6000.0, "2024": 5000.0}),
        ],
        PROFILE,
        only=["ROE"],
        period_current="2025",
        period_prev="2024",
    )
    roe = _by_enum(rows, "ROE")
    averaged = [i for i in roe["inputs"] if i["transform"] == "avg"]
    assert [(i["enum"], i["value"]) for i in averaged] == [("TOTAL_EQUITY", 5500.0)]
