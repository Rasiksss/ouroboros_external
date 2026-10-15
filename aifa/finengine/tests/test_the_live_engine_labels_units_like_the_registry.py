"""Единицу расчётной строки называет реестр — в БОЕВОМ движке тоже.

ЦЕНА (замер 21.08.2026 по всем десяти ячейкам). Подпись единицы делают те же
три производителя расчётных строк, и звали они РАЗНОЕ:

    ratios.py                                 unit_for_calculated(...)
    bulk_extractor/streams_full_context.py    unit_for_calculated(...)
    two_stage_extractor.py                    "%" if "* 100" in formula else деньги  ← БОЕВОЙ

Догадка по виду формулы узнаёт проценты и больше ничего. Из 361 расчётной
записи десяти ячеек расходились 85:

    65 безразмерных отношений  → денежная подпись («Долг/EBITDA 2,4 млн. руб.»,
                                 «Покрытие процентов 5,1 млн. руб.»)
    20 оборачиваемостей в днях → денежная подпись («Денежный цикл 61 млн. руб.»)

Функция `unit_for_calculated` берёт слово реестра (`unit_kind`), потом сотню в
формуле, и только потом выводит по структуре — безразмерное остаётся БЕЗ
подписи, потому что пустая честнее ложной денежной.

ПОЧЕМУ ЭТО НЕ ЛОВИЛОСЬ РАНЬШЕ. `calculated_meta` в боевой движок вообще не
доезжала — читать `unit_kind` было неоткуда, и подпись сочинялась на месте.
Карту привезла правка приведения к году (R2); эта читает её вторую половину.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ — ДОСТИЖИМОСТЬ, а не текст в файле: движок крутится
целиком, подделан ровно шлюз LLM, единица читается из готового события
`metric`. Харнесс общий с `test_quarterly_return_is_raised_to_the_year.py`.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 21.08.2026, каждая покраснела):
  1. вернуть `unit_calc = "%" if "* 100" in formula else default_unit` →
     падают `test_a_dimensionless_ratio_is_not_labelled_money` и
     `test_days_of_turnover_are_labelled_days`;
  2. `calc_meta.get(enum, {}).get("unit_kind", "")` → `""` (карта доехала, слово
     не читается) → падает `test_days_of_turnover_are_labelled_days`;
  3. подменить `unit_for_calculated` на `lambda f, d, k: d` →
     падают все три.
"""
from __future__ import annotations

import pytest

from finengine.skills.bulk_extractor import unit_for_calculated
from finengine.skills.metric_schema import registry_for_profile

from tests.test_quarterly_return_is_raised_to_the_year import _registry_for, _row, _run

# Три класса единиц в одной ячейке. Формулы и `unit_kind` — НАСТОЯЩИЕ,
# сужается только состав: подмени их здесь, и сторож проверял бы свою выдумку.
_PERCENT = "NET_INCOME_MARGIN"          # * 100 в формуле        → «%»
_RATIO = "DEBT_TO_EQUITY_RATIO"         # безразмерное отношение → пусто
_DAYS = "INVENTORIES_TURNOVER"          # unit_kind=days         → «дн.»

_INPUTS = {
    "NET_INCOME": {"3 мес. 2025": 400.0},
    "SALES_REVENUE": {"3 мес. 2025": 2000.0},
    "TOTAL_DEBT": {"3 мес. 2025": 5000.0},
    "TOTAL_EQUITY": {"3 мес. 2025": 10000.0},
    "COSTS_EXPENSES": {"3 мес. 2025": 1200.0},
    "INVENTORIES": {"3 мес. 2025": 300.0},
}


def _units() -> dict[str, str]:
    """Единицы, которые боевой движок напечатал у трёх расчётных строк."""
    registry = _registry_for(list(_INPUTS) + [_PERCENT, _RATIO, _DAYS])
    rows = _run(pytest.MonkeyPatch(), values=_INPUTS, registry=registry)
    return {e: str(_row(rows, e).get("unit") or "") for e in (_PERCENT, _RATIO, _DAYS)}


def test_a_percent_is_still_labelled_percent():
    assert _units()[_PERCENT] == "%"


def test_a_dimensionless_ratio_is_not_labelled_money():
    got = _units()[_RATIO]
    assert got == "", (
        f"безразмерное отношение подписано «{got}» — читатель видит "
        "«Долг/капитал 0,5 млн. руб.» и принимает отношение за деньги"
    )


def test_days_of_turnover_are_labelled_days():
    got = _units()[_DAYS]
    assert got == "дн.", (
        f"оборачиваемость подписана «{got}» вместо «дн.» — слово реестра "
        "`unit_kind` до боевого движка не доехало"
    )


def test_the_price_of_the_old_guess_stays_recorded():
    """Не сторож правки, а ЗАКРЕПЛЁННАЯ ЦЕНА дефекта — 85 записей из 361.

    Три сторожа выше держат поведение движка. Этот держит ЧИСЛО: если состав
    реестра поедет и расхождений станет заметно меньше, читатель этого файла
    должен узнать, что цена другая, а не верить строке из шапки. Зелёный он и
    до правки, и после — так и задумано, и поэтому он назван мерой, а не
    сторожем.
    """
    default_by_cell = {
        "ifrs_bank_ru": "млн. руб.", "ifrs_nf_ru": "млн. руб.",
        "rsbu_bank_ru": "млн. руб.", "rsbu_nf_ru": "млн. руб.",
        "ifrs_bank_en": "млн. $", "ifrs_nf_en": "млн. $",
        "us_gaap_bank_en": "млн. $", "us_gaap_nf_en": "млн. $",
        "ind_as_nf_en": "INR", "ind_gaap_bank_en": "INR",
    }
    guessed = []
    for cell, default_unit in default_by_cell.items():
        registry = registry_for_profile(cell)
        meta = registry.get("calculated_meta") or {}
        for enum, _name, formula, _deps in registry["calculated"]:
            key = str(enum).strip().upper()
            by_registry = unit_for_calculated(
                formula, default_unit, meta.get(key, {}).get("unit_kind", "")
            )
            by_guess = "%" if ("* 100" in formula or "× 100" in formula) else default_unit
            if by_registry != by_guess:
                guessed.append(f"{cell}/{key}: реестр «{by_registry}», догадка «{by_guess}»")
    assert guessed, (
        "сверять нечего: догадка по формуле совпала с реестром везде — "
        "значит сузился реестр, и сторож перестал что-либо охранять"
    )
    assert len(guessed) >= 80, (
        f"расхождений догадки с реестром {len(guessed)}, ожидалось ~85 — "
        "состав реестра поехал, пересчитать цену дефекта"
    )
