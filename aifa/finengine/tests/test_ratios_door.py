"""Расчёт коэффициентов как отдельный шаг: дверь `skills/ratios.py`.

ЗАЧЕМ ЭТИ СТОРОЖА. Канон §4 выносит расчёт в отдельный шаг маршрута: скрипт
навыка `aifa-calc` берёт таблицу прогона файлом и дописывает в неё расчётные
строки. Дверь для него — `compute_ratios`. Три вещи она обязана делать и одну
не делать:

  * считать ТЕМ ЖЕ вычислителем, что движок, — иначе получим два числа под
    одним именем, уже с другого конца (#163);
  * отдавать строки ДВИЖКОВОЙ формы — по `is_calculated` их узнаёт и
    счётчик прогресса станции, и раскладка роли;
  * не пересчитывать то, что уже стоит в таблице, — иначе в одной таблице
    окажутся две строки с одним именем и разными числами;
  * не молчать про непонятое: `resolve_requested` возвращает отброшенные
    имена отдельно, а `check_shape` отказывает ДО реестра.
"""
from __future__ import annotations

import pytest

from finengine.skills import ratios, requested_metrics

CELL = "ifrs_nf_ru"


def _row(enum: str, cur: float, prev: float) -> dict:
    return {
        "enum": enum, "name_ru": enum, "value_current": cur, "value_prev": prev,
        "unit": "млн руб.", "is_calculated": False, "found": True,
        "period_values": [{"period": "FY 2024", "value": cur, "unit": "млн руб."},
                          {"period": "FY 2023", "value": prev, "unit": "млн руб."}],
    }


TABLE = [_row("SALES_REVENUE", 1000.0, 800.0),
         _row("NET_INCOME", 100.0, 60.0),
         _row("TOTAL_EQUITY", 500.0, 450.0)]


def test_rows_are_engine_shaped_and_carry_the_formula():
    rows = ratios.compute_ratios(TABLE, CELL)
    assert rows, "реестр ячейки непустой, а не посчиталось ничего"
    margin = next(r for r in rows if r["enum"] == "NET_INCOME_MARGIN")
    assert margin["value_current"] == pytest.approx(10.0), (
        "маржа посчитана не по реестровой формуле — где-то завелась вторая арифметика")
    assert margin["is_calculated"] is True and margin["formula"], (
        "без `is_calculated` и формулы строку не отличит ни счётчик прогресса, "
        "ни раскладка роли — она встанет в таблицу как извлечённая")
    assert set(margin) >= {"type", "enum", "name_ru", "value_current", "value_prev",
                           "period_values", "unit", "found"}, "форма строки не движковая"


def test_a_metric_already_in_the_table_is_not_computed_twice():
    """#163: enum, стоящий в таблице, второй раз не заводим."""
    with_capex = [*TABLE, _row("CAPEX", 42.0, 40.0)]
    assert not [r for r in ratios.compute_ratios(with_capex, CELL) if r["enum"] == "CAPEX"], (
        "CAPEX посчитан поверх извлечённого — в таблице окажутся две строки "
        "с одним именем и разными числами")
    assert [r for r in ratios.compute_ratios(TABLE, CELL) if r["enum"] == "CAPEX"], (
        "предпосылка теста ушла: CAPEX должен быть расчётным, когда его нет в таблице")


def test_only_narrows_the_set_and_nothing_else():
    rows = ratios.compute_ratios(TABLE, CELL, only=["ROE"])
    assert [r["enum"] for r in rows] == ["ROE"]
    assert rows[0]["value_current"] == pytest.approx(
        next(r for r in ratios.compute_ratios(TABLE, CELL)
             if r["enum"] == "ROE")["value_current"]), (
        "сужение состава изменило ЧИСЛО — значит `only` попал во входы формулы")


def test_segment_rows_never_feed_the_formulas():
    """Сегмент — другой разрез. Подмешать его значит посчитать рентабельность
    компании по одному сегменту, и заметить это будет нечем."""
    poisoned = [*TABLE, {**_row("NET_INCOME", 1.0, 1.0), "dimension": "segment",
                         "segment": "Металлургия"}]
    assert (ratios.compute_ratios(poisoned, CELL)
            == ratios.compute_ratios(TABLE, CELL))


def test_unresolved_names_come_back_named_not_dropped():
    found, unknown = requested_metrics.resolve_requested(
        ["NET_INCOME", "чистая прибыль", "зюзюблик"], CELL)
    assert "NET_INCOME" in found
    assert found.count("NET_INCOME") == 1, "дубль просьбы не схлопнулся"
    assert unknown == ["зюзюблик"], (
        "непонятое имя выкинуто молча — пользователь просил четыре показателя, "
        "получит три, и разницу заметит только тот, кто пересчитает строки")


@pytest.mark.parametrize("bad", [
    ["A" * 65],
    ["NET_INCOME; rm -rf /\n"],
    ["ENUM\tX"],
    [""],
])
def test_shape_is_refused_before_the_registry(bad):
    """Строка уезжает в командную строку станции: кавычка с переводом строки
    внутри имени — это чужая команда, а не показатель."""
    with pytest.raises(requested_metrics.RequestShapeError):
        requested_metrics.check_shape(bad)


def test_a_normal_ask_passes_the_shape_check():
    requested_metrics.check_shape(["NET_INCOME", "чистая прибыль"])
