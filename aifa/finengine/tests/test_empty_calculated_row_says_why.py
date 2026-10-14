"""Пустая расчётная строка называет, каких входов ей не хватило.

ЦЕНА (Б5, 19.08.2026). Навык расчёта уже пишет причину в саму строку —
`calc_run.py` кладёт в `calc_missing` перечень показателей формулы, которых в
таблице прогона не нашлось, и делает это ИМЕННО в строке, а не в stdout: «в
stdout помещается дюжина строк, а разбирать почему пусто будут по файлу».

Но до экрана причина не доезжала: `station_turn._ROW_KEYS` режет событие хода
по белому списку, и `calc_missing` в нём не было. Пользователь видел строку с
формулой, без числа и без объяснения — неотличимо от «извлечение промахнулось».

ПРОВЕРКА СТОРОЖА ОТКАТОМ (проделана 19.08): убрать `"calc_missing"` из
`_ROW_KEYS` → тест краснеет.
"""
from __future__ import annotations

from finengine.api import station_turn

EMPTY_CALCULATED_EVENT = {
    "type": "metric",
    "enum": "RATIO_NET_DEBT_EBITDA",
    "name_ru": "Чистый долг / EBITDA",
    "value_current": None,
    "value_prev": None,
    "period_values": [],
    "unit": "",
    "found": False,
    "is_calculated": True,
    "formula": "NET_DEBT / EBITDA",
    "inputs": [],
    "calc_missing": ["NET_DEBT"],
}


def test_the_reason_a_formula_stayed_silent_reaches_the_screen():
    assert station_turn._row_body(EMPTY_CALCULATED_EVENT)["calc_missing"] == ["NET_DEBT"]
