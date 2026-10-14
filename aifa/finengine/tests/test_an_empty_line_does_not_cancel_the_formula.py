"""Показателя нет печатной строкой — его считает формула (Б0, 19.08).

ДЕФЕКТ, НАЙДЕННЫЙ ПО ДАННЫМ ПРОГОНА. Северсталь, 1 кв. 2025, ячейка
`ifrs_nf_ru`, 122 строки, прогон закрылся `done`. В таблице:

    OPERATING_PROFIT           28 355   найдено
    DEPRECIATION_AMORTIZATION   9 495   найдено
    EBITDA                        —     ПУСТО

EBITDA = операционная прибыль + амортизация, формула лежит в реестре ячейки.
Оба входа извлечены. Строка пуста.

ПРИЧИНА — ДВА ЗАПРЕТА, КАЖДЫЙ СО СВОИМ СМЫСЛОМ. Шесть показателей ячейки
заведены И извлекаемыми, И расчётными (EBITDA, CAPEX, валовая прибыль,
дивиденды, права пользования активами, покупки активов). Чтобы под одним именем
не оказалось двух чисел (#163), такие показатели лишали расчётной половины:

  * `two_stage_extractor._prepare_common_scope` выбрасывал формулу ДО прогона —
    по одному лишь факту, что имя есть в списке извлекаемых;
  * `ratios.compute_ratios` считал показатель «уже стоящим в таблице» по
    наличию СТРОКИ, а не значения.

В обоих местах пустая строка отменяла формулу. Печатной строки «EBITDA» в
отчётности по МСФО обычно нет вовсе — значит для неё запрет срабатывал всегда.

ПРАВИЛО ТЕПЕРЬ ОДНО: извлечённое старше расчётного, но только если оно ЕСТЬ.
Нашли строку в отчёте — формула молчит. Не нашли — считает и занимает то же
место, а не заводит второе. Двух чисел под одним именем по-прежнему не бывает.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в `_prepare_common_scope` безусловный
выброс формул двойных enum'ов — красен `test_the_engine_keeps_both_halves`;
убрать условие `found` из `present` в `ratios.py` — красен
`test_an_empty_row_does_not_block_the_formula`.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path

import pytest

from finengine.skills import ratios
from finengine.skills import two_stage_extractor as ts
from finengine.skills.metric_schema import registry_for_profile

CELL = "ifrs_nf_ru"


def _row(enum: str, cur, prev, *, found=True) -> dict:
    return {
        "enum": enum, "name_ru": enum, "value_current": cur, "value_prev": prev,
        "unit": "млн руб.", "is_calculated": False, "found": found,
        "period_values": ([{"period": "FY 2024", "value": cur, "unit": "млн руб."},
                           {"period": "FY 2023", "value": prev, "unit": "млн руб."}]
                          if found else []),
    }


# Числа прогона Северстали, из-за которого сторож и написан.
INPUTS = [_row("OPERATING_PROFIT", 28355.0, 25000.0),
          _row("DEPRECIATION_AMORTIZATION", 9495.0, 9000.0)]
EMPTY_EBITDA = _row("EBITDA", None, None, found=False)


def test_the_case_is_still_real_in_the_registry():
    """Предпосылка: EBITDA у ячейки И извлекаемая, И расчётная."""
    reg = registry_for_profile(CELL)
    dual = ts._dual_nature_enums(reg["extractable"], reg["calculated"])
    assert "EBITDA" in dual and "GROSS_PROFIT" in dual, (
        f"в {CELL} больше нет двойных показателей — сторож проверяет "
        f"несуществующий случай, а не защищает: {sorted(dual)}")
    assert not ts._dual_nature_enums(reg["extractable"], []), (
        "функция двойных enum'ов возвращает что-то на пустом списке формул")


def test_an_empty_row_does_not_block_the_formula():
    """Строка EBITDA пустая, входы есть — считаем."""
    rows = ratios.compute_ratios([*INPUTS, EMPTY_EBITDA], CELL)
    ebitda = [r for r in rows if r["enum"] == "EBITDA"]
    assert ebitda, (
        "пустая строка EBITDA отменила формулу — ровно то, что случилось у "
        "владельца 19.08: операционная прибыль и амортизация в таблице, "
        "EBITDA пуста")
    assert ebitda[0]["value_current"] == pytest.approx(37850.0)


def test_a_row_with_a_value_still_blocks_the_formula():
    """Смягчение #163 не отменено: извлечённое число формулой не перебивают."""
    filled = _row("EBITDA", 40000.0, 38000.0)
    rows = ratios.compute_ratios([*INPUTS, filled], CELL)
    assert not [r for r in rows if r["enum"] == "EBITDA"], (
        "EBITDA посчитана поверх извлечённой — в таблице окажутся два числа "
        "под одним именем")


def _calc_run():
    spec = importlib.util.spec_from_file_location(
        "calc_run", Path("station-skills/aifa-calc/scripts/calc_run.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_computed_row_replaces_the_empty_one(tmp_path: Path):
    """Расчёт навыком: строка одна, и она со значением."""
    calc_run = _calc_run()

    envelope = {"run": {"cell": CELL, "main_period": "FY 2024", "prev_period": "FY 2023"},
                "metrics": [*INPUTS, EMPTY_EBITDA]}
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

    assert calc_run._run(str(path), "", "", "", False) == 0
    out = json.loads(path.read_text(encoding="utf-8"))["metrics"]
    ebitda = [r for r in out if r.get("enum") == "EBITDA"]
    assert len(ebitda) == 1, (
        f"строк EBITDA {len(ebitda)}, а должна быть одна: посчитанное обязано "
        f"заместить пустую строку, а не лечь рядом с ней")
    assert ebitda[0]["value_current"] == pytest.approx(37850.0)


def test_a_formula_that_stays_silent_does_not_add_a_second_empty_row(tmp_path: Path):
    """Не посчиталось — строка остаётся ОДНА, и в ней написано, чего не хватило.

    🔴 ЭТОТ СЛУЧАЙ ПОЙМАН НЕ ТЕСТОМ, А ПРОГОНОМ ПО НАСТОЯЩЕЙ ТАБЛИЦЕ. Первая
    редакция правки замещала пустую строку только при удачном расчёте, а
    неудачный ложился рядом — и `RIGHT_OF_USE_ASSETS` появился в таблице
    Северстали дважды, оба раза пустой. То есть ровно #163, от которого правка
    и защищала, только с другого конца.
    """
    calc_run = _calc_run()
    empty_rou = _row("RIGHT_OF_USE_ASSETS", None, None, found=False)
    envelope = {"run": {"cell": CELL, "main_period": "FY 2024", "prev_period": "FY 2023"},
                "metrics": [*INPUTS, empty_rou]}
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

    assert calc_run._run(str(path), "", "", "", False) == 0
    out = json.loads(path.read_text(encoding="utf-8"))["metrics"]
    rou = [r for r in out if r.get("enum") == "RIGHT_OF_USE_ASSETS"]
    assert len(rou) == 1, (
        f"строк RIGHT_OF_USE_ASSETS {len(rou)}: пустая расчётная легла рядом с "
        f"пустой извлечённой — одно имя, две строки")
    assert rou[0].get("calc_missing") == ["SHORT_TERM_ROU", "LONG_TERM_ROU"], (
        "строка одна, но причина отказа потерялась: «пусто» без «чего не "
        "хватило» разбирать нечем")


def test_the_engine_keeps_both_halves():
    """СТОРОЖ ДОСТАВКИ: движок больше не выбрасывает формулу до прогона."""
    src = inspect.getsource(ts._prepare_common_scope)
    assert "_dual_nature_enums(extractable, calculated_defs)" in src, (
        "в подготовке прогона не считаются двойные enum'ы — значит либо формулы "
        "снова выброшены, либо в таблицу поедут две строки с одним именем")
    assert "not in extracted_enum_set" not in src, (
        "вернулся безусловный выброс формул двойных enum'ов: EBITDA снова "
        "нечем посчитать, когда её нет печатной строкой")


def test_the_engine_holds_the_empty_row_until_the_formula_answers():
    """СТОРОЖ ДОСТАВКИ: пустая строка придержана и отдана, если формула молчит."""
    src = inspect.getsource(ts._extract_stream_two_stage_full_context)
    assert "deferred_dual[enum] = row" in src, (
        "пустая строка двойного enum'а уезжает сразу — рядом с ней встанет "
        "расчётная, и в таблице будет два раза одно имя")
    assert "held = deferred_dual.pop(enum, None)" in src, (
        "придержанную строку никто не забирает: не посчиталось — и показатель "
        "исчез из таблицы вовсе, вместо честного «искали, не нашли»")
