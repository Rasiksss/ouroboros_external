"""Отказ СВОЕЙ формулы обязан назвать причину.

Куплено прогоном владельца `a89c6a9f` (Северсталь, 1 кв. 2025). Он попросил
вслух: «доходность на акцию по свободному денежному потоку». Модель подала свою
формулу через `--extra-calculated` — `FCF / SHARES_OUTSTANDING` — и это уже
правильно: число шло в таблицу прогоном, а не мимо неё через `fe_calc`.

Но `SHARES_OUTSTANDING` она не заказала и рыночным не пометила. Строка встала в
таблицу ПУСТОЙ, а `calc_missing` — пустым списком. То есть по прямой просьбе
человек получил пустую клетку и НИ СЛОВА о том, чего не хватило.

Причина: карта «формула → входы» строилась только по реестру ячейки, а своя
формула реестру неизвестна по устройству. `deps_by_enum.get(enum, [])` отдавал
пустой список, `_missing_inputs` честно возвращал по нему пустоту — и диагноз,
написанный ради этого самого случая, молчал.

⚠️ Пустая клетка без объяснения хуже отказа: отказ говорит, что делать дальше,
а пустота неотличима от «показателя нет в отчёте».
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_путь = (Path(__file__).resolve().parents[1]
         / "station-skills" / "aifa-calc" / "scripts" / "calc_run.py")
_спека = importlib.util.spec_from_file_location("calc_run_под_тестом", _путь)
calc_run = importlib.util.module_from_spec(_спека)
_спека.loader.exec_module(calc_run)

# Реестр отдаёт расчётные кортежами (enum, name_ru, formula, deps).
РЕЕСТР = [("WORKING_CAPITAL", "Оборотный капитал",
           "TOTAL_CURRENT_ASSETS - TOTAL_CURRENT_LIABILITIES",
           ["TOTAL_CURRENT_ASSETS", "TOTAL_CURRENT_LIABILITIES"])]
СВОЯ = {"enum": "FCF_PER_SHARE", "name_ru": "FCF на акцию",
        "formula": "FCF / SHARES_OUTSTANDING",
        "deps": ["FCF", "SHARES_OUTSTANDING"], "unit_kind": "money"}


def test_a_custom_formula_knows_its_inputs() -> None:
    """Без этого отказ своей формулы выходит без причины."""
    карта = calc_run.deps_map(РЕЕСТР, [СВОЯ])
    assert карта.get("FCF_PER_SHARE") == ["FCF", "SHARES_OUTSTANDING"], (
        "входы своей формулы не попали в карту — значит calc_missing по ней "
        "выйдет пустым, и пустая клетка приедет к человеку без объяснения"
    )


def test_the_missing_input_is_named_not_swallowed() -> None:
    """Сквозная проверка: чего нет в таблице — то и названо."""
    карта = calc_run.deps_map(РЕЕСТР, [СВОЯ])
    есть = {"FCF", "TOTAL_CURRENT_ASSETS", "TOTAL_CURRENT_LIABILITIES"}
    нет = calc_run._missing_inputs(карта.get("FCF_PER_SHARE", []), есть)
    assert нет == ["SHARES_OUTSTANDING"], (
        f"причина отказа {нет!r}, а ожидали поимённо SHARES_OUTSTANDING — "
        "именно его не хватило в живом прогоне a89c6a9f"
    )


def test_the_registry_still_wins_a_name_clash() -> None:
    """Своя формула не подменяет реестровую втихую.

    `own_calc_defs` такие имена и так отклоняет. Правило продублировано здесь
    намеренно: если однажды его снимут там, подмена определения не должна
    пройти молча через карту входов.
    """
    подмена = {"enum": "WORKING_CAPITAL", "formula": "TOTAL_ASSETS",
               "deps": ["TOTAL_ASSETS"]}
    карта = calc_run.deps_map(РЕЕСТР, [подмена])
    assert карта["WORKING_CAPITAL"] == ["TOTAL_CURRENT_ASSETS", "TOTAL_CURRENT_LIABILITIES"], (
        "своя формула перебила реестровое определение — подмена стала бы "
        "невидимой: имя то же, число другое"
    )


def test_registry_formulas_are_not_lost_when_there_are_no_custom_ones() -> None:
    """Обычный ход — своих формул нет; карта обязана остаться прежней."""
    карта = calc_run.deps_map(РЕЕСТР, [])
    assert карта == {"WORKING_CAPITAL": ["TOTAL_CURRENT_ASSETS", "TOTAL_CURRENT_LIABILITIES"]}
