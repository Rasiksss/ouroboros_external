"""Из одного вычитаемого величина не собирается.

ЧТО СЛУЧИЛОСЬ. У CAPEX (и вообще у показателей из `_PARTIAL_ZERO_FORMULA_ENUMS`)
недостающие входы формулы достраиваются нулём, если найден хоть один вход.
Задумано это для недостающего СЛАГАЕМОГО: компания не раскрыла покупку НМА —
считаем её нулём, знак и порядок величины сохраняются.

Но формула CAPEX — разность. Если из «покупки − продажи» нашлись ТОЛЬКО
продажи, ноль подставляется в БАЗУ, и в клетку CAPEX уезжает минус выручка от
продажи имущества. Это не заниженный capex, это выдуманное число с обратным
знаком, и дальше оно уходит в `FREE_CASH_FLOW = NET_OPERATING_CASH_FLOW − CAPEX`
и в FCFF, завышая их.

ЦЕНА, СНЯТАЯ С РАЗМЕТКИ 20.08.2026 (`benchmarks/vendor/.../golden/*/gt/*.csv`):
десять период-случаев на пяти документах, где `ASSET_SALES_PROCEEDS` размечен, а
ни одного слагаемого покупок нет. Худший — Черкизово-2021: продажи 323 и 364,
размеченный CAPEX 12 218 и 17 431; формула дала бы −323 и −364 — мимо знака и в
сорок раз мимо величины. В шести случаях из десяти (РЖД, Japan Airlines, Exxon,
Intel) печатной строки CAPEX в отчёте нет вовсе — значит приоритет «извлечённое
старше расчётного» выдуманное значение НЕ перекрыл бы, и клетка, до правки
пустая, приехала бы заполненной неверно.

ПРАВИЛО. Достраиваем нулём только тогда, когда найдено хотя бы одно слагаемое
(вход под знаком «плюс»). Не найдено ни одного — формулы для этого периода нет.

ПОЧЕМУ СТОРОЖ ОТДЕЛЬНЫМ ФАЙЛОМ, А НЕ СТРОКОЙ В `test_formula_eval`. Там снимок
`GOLDEN` считает формулы на ПОЛНЫХ синтетических входах — частично заполненный
вход туда не приходит вовсе, и дыру он не видел и не увидит.

ПРОВЕРКА ОТКАТОМ — мутации, прогнанные 20.08.2026:
* убрать `and (present & _added_formula_tokens(formula))` из
  `_optional_zero_deps_for_formula` — краснеют пять параметризаций
  `test_only_the_subtrahend_leaves_the_cell_empty` («вернул -15.0 вместо None»);
* в `_collect_added_tokens` пустить правую ветку `ast.Sub` с тем же знаком —
  краснеют те же пять плюс `test_parenthesised_minus_flips_the_whole_group`;
* заменить условие на `if present and False` — краснеют
  `test_missing_deduction_is_still_zero_filled` и `test_all_plus_formulas_kept_partial_zero`,
  то есть возврат реколла проверяется отдельно от запрета.
"""
from __future__ import annotations

import pytest

from finengine.skills.bulk_extractor import _calculate_metrics, _optional_zero_deps_for_formula
from finengine.skills.bulk_extractor.formula_engine import _added_formula_tokens
from finengine.skills.metric_schema import registry_for_profile

# Ячейки, где CAPEX заведён расчётным. rsbu_nf_ru пришла сюда 20.08.2026 вместе
# с формулой `COMBINED_ASSET_PURCHASES − ASSET_SALES_PROCEEDS`; у остальных
# четырёх формула трёхчленная и дыра была ДО этой ветки, а не заведена ею.
CELLS_WITH_CAPEX = ["rsbu_nf_ru", "ifrs_nf_ru", "ifrs_nf_en", "us_gaap_nf_en", "ind_as_nf_en"]


@pytest.mark.parametrize("cell", CELLS_WITH_CAPEX)
def test_only_the_subtrahend_leaves_the_cell_empty(cell: str):
    """Нашлись одни продажи — CAPEX пуст, а не равен минус продажам."""
    result = _calculate_metrics(
        registry_for_profile(cell)["calculated"], {"ASSET_SALES_PROCEEDS": 15.0}
    )
    assert result.get("CAPEX") is None, (
        f"{cell}/CAPEX: из формулы найдены ТОЛЬКО продажи имущества, а "
        f"вычислитель вернул {result.get('CAPEX')!r}. Это выдуманная величина с "
        f"обратным знаком: она уедет в FREE_CASH_FLOW и завысит его."
    )


@pytest.mark.parametrize("cell", CELLS_WITH_CAPEX)
def test_missing_deduction_is_still_zero_filled(cell: str):
    """Нашлись покупки, продажи не раскрыты — считаем: реколл не потерян."""
    result = _calculate_metrics(
        registry_for_profile(cell)["calculated"],
        {"COMBINED_ASSET_PURCHASES": 90.0, "ASSET_PURCHASES": 90.0},
    )
    assert result.get("CAPEX") == pytest.approx(90.0), (
        f"{cell}/CAPEX: слагаемое найдено, вычитаемое нет — недостающее "
        f"вычитаемое обязано достроиться нулём, а вычислитель вернул "
        f"{result.get('CAPEX')!r}. Запрет уехал шире, чем задуман."
    )


def test_all_plus_formulas_kept_partial_zero():
    """У формул без вычитания правило ничего не поменяло.

    DIVIDENDS и TOTAL_DEBT — чистые суммы: любой найденный вход и есть
    слагаемое, поэтому подстановка нуля работает как работала.
    """
    deps = _optional_zero_deps_for_formula(
        "DIVIDENDS",
        "DIVIDENDS_SHAREHOLDERS + DIVIDENDS_PAID_NONCONTROLLING_INTERESTS",
        {"DIVIDENDS_PAID_NONCONTROLLING_INTERESTS": 4.0},
    )
    assert deps == {"DIVIDENDS_SHAREHOLDERS"}


def test_parenthesised_minus_flips_the_whole_group():
    """«A − (B + C)»: слагаемое здесь одно, скобка перевернула знак двум."""
    assert _added_formula_tokens("A - (B + C)") == {"A"}
    assert _added_formula_tokens("ASSET_PURCHASES + INTANGIBLE_ASSET_PURCHASES"
                                 " - ASSET_SALES_PROCEEDS") == {
        "ASSET_PURCHASES", "INTANGIBLE_ASSET_PURCHASES"
    }


def test_broken_formula_does_not_crash_the_run():
    """Битую формулу приносит редактор реестра — прогон обязан устоять."""
    assert _added_formula_tokens("A + + ") == set()
