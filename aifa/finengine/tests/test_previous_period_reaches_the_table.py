"""Предыдущий период доезжает до таблицы рабочей области (#205).

ЗАЧЕМ ЭТОТ ФАЙЛ. Прогон владельца по Русгидро 09.08.2026: в таблице рабочей
области колонка FY 2023 пустая ВО ВСЕХ строках, Δ тоже пустая — при том что в
markdown-таблице, ушедшей в чат тем же прогоном, оба периода были на месте
(«33 381 | 24 167», «26.01 | 25.88», «0.89 | 1.03»).

ПРИЧИНА. Две раскладки читают РАЗНЫЕ поля показателя. Markdown-таблица чата
берёт `value_current`/`value_prev`; таблица рабочей области — `period_values`.
Запасной путь в `_period_values` стоял под условием `if not out:` — то есть
`value_prev` смотрели ТОЛЬКО когда `period_values` пуст ЦЕЛИКОМ. Движок же
заполняет его ЧАСТИЧНО: запись за текущий год есть, за предыдущий нет.

Следствие на экране: колонка FY 2023 существует (её создали те немногие
показатели, у которых запись за 2023 была), а значений в ней почти нигде нет.
Молча: обе таблицы «работают», просто показывают разное.

ПОЧЕМУ ЭТО ДОРОГО. Без предыдущего периода вхолостую работает вся динамическая
часть методики: разделы «динамика» и «изменение», статус-флаги («ухудшается»,
«рост с оговоркой») и почти все выводы опираются на сравнение двух периодов.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть `if not out:` вокруг запасного пути в
`formatter._period_values` — `test_partial_period_values_still_show_previous`
обязан покраснеть.
"""
from __future__ import annotations

import pytest

from finengine.roles.formatter import build_formatted_table
from finengine.roles.scope import build_engine_scope

META = {"period_current": "2024", "period_prev": "2023",
        "company_type": "non_financial", "standard": "ifrs", "language": "ru"}


def _table(metrics: list[dict], role: str = "die"):
    plan, _ = build_engine_scope(role, "non_financial", "ifrs_nf_ru")
    return build_formatted_table(plan=plan, metrics=metrics, meta=META)


def test_partial_period_values_still_show_previous():
    """ГЛАВНОЕ: запись только за текущий год + value_prev → в таблице ОБА года."""
    t = _table([{
        "enum": "SALES_REVENUE", "name_ru": "Выручка", "found": True, "page": 12,
        "period_values": [{"period": "2024", "value": 579817}],
        "value_current": 579817, "value_prev": 510325,
    }])
    assert "2023" in [c.get("key") for c in t["columns"]], (
        "колонки предыдущего периода нет — сравнивать будет не с чем"
    )
    assert t["rows"][0]["values"] == {"2023": 510325, "2024": 579817}


def test_period_values_are_not_overwritten_by_the_fallback():
    """`period_values` — главный источник: менее точное его не перетирает.

    Иначе починка одного дефекта завела бы другой, хуже: в таблице появились бы
    числа, которых движок по этому периоду не извлекал.
    """
    t = _table([{
        "enum": "SALES_REVENUE", "name_ru": "Выручка", "found": True,
        "period_values": [{"period": "2024", "value": 579817},
                          {"period": "2023", "value": 510325}],
        "value_current": 111, "value_prev": 222,
    }])
    assert t["rows"][0]["values"] == {"2023": 510325, "2024": 579817}


def test_no_period_values_at_all_still_works():
    """Старый случай (записей нет вовсе) не должен сломаться починкой."""
    t = _table([{
        "enum": "SALES_REVENUE", "name_ru": "Выручка", "found": True,
        "value_current": 579817, "value_prev": 510325,
    }])
    assert t["rows"][0]["values"] == {"2023": 510325, "2024": 579817}


def test_missing_previous_stays_missing():
    """Нет предыдущего нигде — не выдумываем: пусто остаётся пустым."""
    t = _table([{
        "enum": "SALES_REVENUE", "name_ru": "Выручка", "found": True,
        "period_values": [{"period": "2024", "value": 579817}],
        "value_current": 579817,
    }])
    vals = t["rows"][0]["values"]
    assert vals.get("2024") == 579817
    assert not vals.get("2023"), f"предыдущий период выдуман: {vals}"


# У каждой раскладки свой набор строк, поэтому берём показатель, который в ней
# заведомо есть: дефект был в ОБЩЕЙ сборке значений, и проверить надо обе.
@pytest.mark.parametrize("role,enum,name", [
    ("die", "SALES_REVENUE", "Выручка"),
    ("risks", "TOTAL_ASSETS", "Итого активы"),
])
def test_both_layouts_see_the_previous_period(role, enum, name):
    t = _table([{
        "enum": enum, "name_ru": name, "found": True,
        "period_values": [{"period": "2024", "value": 1000}],
        "value_current": 1000, "value_prev": 900,
    }], role=role)
    row = next((r for r in t["rows"] if r.get("enum") == enum), None)
    assert row is not None, f"показателя {enum} нет в раскладке {role} — тест устарел"
    assert row["values"].get("2023") == 900, f"раскладка {role} потеряла предыдущий период"
