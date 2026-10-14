"""Своя формула сама заказывает свои входы — и не врёт про недостающие.

Куплено прогоном владельца `61a6c634` (Северсталь, 25.08.2026). Он попросил
вслух пять коэффициентов. Модель подала четыре своими формулами — уже правильно,
через прогон в таблицу. На экран приехали ЧЕТЫРЕ ПУСТЫЕ СТРОКИ:

    ROCE                  нет TOTAL_ASSETS
    DA_REVENUE_RATIO      нет DEPRECIATION_AMORTIZATION
    COVENANT_HEADROOM_3X  нет RATIO_NET_DEBT_EBITDA
    FCF_PER_SHARE         нет FREE_CASH_FLOW, SHARES_OUTSTANDING

Два разных дефекта, оба наши.

ПЕРВЫЙ: входы своих формул не заказывал НИКТО. Разворот входов
(`_expand_deps`) работал только для реестровых формул — своя реестру неизвестна
по устройству. Файл `--extra-calculated` ехал мимо извлечения, прямо в расчёт.
Требовать перечисления руками мы уже пробовали телом навыка: модель делает это
для одних формул и забывает для других. `deps` она объявляет ВСЕГДА — без них
валидатор формулу не примет, — значит заказ выводится из объявленного.

ВТОРОЙ: `FREE_CASH_FLOW` в том прогоне ПОСЧИТАЛСЯ и имел значение, а причина
отказа всё равно назвала его недостающим. Набор «добытого» строился по одним
извлечённым строкам. Ложная причина хуже расплывчатой: она посылает модель
переизвлекать то, что уже лежит строкой выше.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from finengine.roles.scope import build_engine_scope

_путь = (Path(__file__).resolve().parents[1]
         / "station-skills" / "aifa-calc" / "scripts" / "calc_run.py")
_спека = importlib.util.spec_from_file_location("calc_run_для_входов", _путь)
calc_run = importlib.util.module_from_spec(_спека)
_спека.loader.exec_module(calc_run)

ЯЧЕЙКА = "ifrs_nf_ru"


def _план(requested, own_deps=None):
    plan, _ = build_engine_scope("other", "non_financial", ЯЧЕЙКА,
                                 requested=requested, own_deps=own_deps)
    return plan


def test_a_custom_formula_input_gets_ordered() -> None:
    """Живой случай ROCE: TOTAL_ASSETS назван в deps, но не в `--metrics`."""
    без = _план(["SALES_REVENUE"])
    с = _план(["SALES_REVENUE"], own_deps=["OPERATING_PROFIT", "TOTAL_ASSETS"])
    assert "TOTAL_ASSETS" not in без.extraction_enums, (
        "предпосылка теста неверна: TOTAL_ASSETS попадает в чтение и без своих формул"
    )
    assert "TOTAL_ASSETS" in с.extraction_enums, (
        "вход своей формулы не заказан движку — ровно так ROCE и вернулся пустым"
    )


def test_the_input_is_read_but_not_shown() -> None:
    """Человек просил коэффициент, а не его слагаемые."""
    план = _план(["SALES_REVENUE"], own_deps=["TOTAL_ASSETS"])
    assert "TOTAL_ASSETS" in план.extraction_enums
    assert "TOTAL_ASSETS" not in (план.output_enums or ()), (
        "слагаемое своей формулы вылезло на экран — читаем шире, показываем уже"
    )


def test_a_registry_coefficient_input_pulls_its_own_parts() -> None:
    """Своя формула законно опирается на реестровый коэффициент.

    `COVENANT_HEADROOM_3X = 3.0 - RATIO_NET_DEBT_EBITDA`: заказать сам
    коэффициент мало, у него есть слагаемые.
    """
    план = _план(["SALES_REVENUE"], own_deps=["RATIO_NET_DEBT_EBITDA"])
    прочтём = план.extraction_enums
    assert "RATIO_NET_DEBT_EBITDA" in прочтём
    assert len(прочтём) > 2, (
        f"входы реестрового коэффициента не развёрнуты: читаем {sorted(прочтём)}"
    )


def test_a_name_outside_the_registry_is_ignored_here() -> None:
    """SHARES_OUTSTANDING заводится через `--extra-metrics`, а не отсюда.

    Молча пропускаем: реестр не обязан знать число акций, и попытка заказать
    его движку как реестровый показатель кончилась бы отказом на ровном месте.
    """
    план = _план(["SALES_REVENUE"], own_deps=["SHARES_OUTSTANDING"])
    assert "SHARES_OUTSTANDING" not in план.extraction_enums


def test_a_computed_value_is_not_called_missing() -> None:
    """Второй дефект: посчитанное — тоже добытое."""
    строки = [
        {"enum": "SALES_REVENUE", "is_calculated": False, "value_current": 100.0, "found": True},
        {"enum": "FREE_CASH_FLOW", "is_calculated": True, "value_current": -38609.0},
    ]
    добыто = calc_run.available_inputs(строки)
    нет = calc_run._missing_inputs(["FREE_CASH_FLOW", "SHARES_OUTSTANDING"], добыто)
    assert нет == ["SHARES_OUTSTANDING"], (
        f"причина отказа {нет!r}: FREE_CASH_FLOW посчитан и лежит в таблице — "
        "называть его недостающим значит послать модель переизвлекать своё же"
    )


def test_an_empty_calculated_row_is_not_an_input() -> None:
    """Формула без значения входом не является.

    Иначе причина отказа звучала бы «формула не дала значения» вместо «нет
    CAPEX» — то есть называла бы симптом вместо причины.
    """
    строки = [{"enum": "FREE_CASH_FLOW", "is_calculated": True, "value_current": None}]
    assert calc_run.available_inputs(строки) == set()


def test_a_value_computed_in_this_pass_counts_too() -> None:
    """Своя формула может опираться на реестровую из того же прохода."""
    добыто = calc_run.available_inputs(
        [{"enum": "SALES_REVENUE", "is_calculated": False, "found": True, "value_current": 1.0}],
        [{"enum": "EBITDA", "is_calculated": True, "value_current": 42.0}],
    )
    assert добыто == {"SALES_REVENUE", "EBITDA"}


def test_only_own_formulas_do_not_unleash_the_full_registry() -> None:
    """Заказ из ОДНИХ своих формул сужает чтение до их входов.

    🔴 ПРОГОН ВЛАДЕЛЬЦА 31.08.2026. «Посчитай коэффициент автономии и
    эффективную ставку налога» — оба показателя свои, реестровых слов в запросе
    нет. Мост вернул пустой `requested`, ветка сужения не запускалась вовсе
    (`if requested:`), и роль other увела прогон в ПОЛНУЮ ячейку: человек
    просил два числа — получил извлечение 122 показателей на шестнадцать минут
    и таблицу на 124 строки, которую не заказывал.

    Входы формул модель объявляет всегда (`deps` требует валидатор) — по ним
    состав и сужается: читаем 4, показываем реестровых 0, свои строки добавит
    расчёт.
    """
    from finengine.roles.scope import build_engine_scope

    plan, enums = build_engine_scope(
        "other", "non_financial", "ifrs_nf_ru",
        requested=None,
        own_deps=["TOTAL_EQUITY", "TOTAL_ASSETS", "INCOME_TAX", "EBT"])
    assert enums == {"TOTAL_EQUITY", "TOTAL_ASSETS", "INCOME_TAX", "EBT"}, (
        f"вместо четырёх входов читается {len(enums)}: заказ из одних своих "
        "формул снова уводит прогон в полный реестр")
    assert not plan.output_enums, (
        "в показ попали реестровые строки — человек просил коэффициенты, а не "
        "их слагаемые; свои строки добавляет расчёт")


def test_no_request_at_all_still_reads_the_full_cell() -> None:
    """Отрицательный контроль: «извлеки метрики» без сужений — полная ячейка.

    Сужение по own_deps не должно было задеть обычный полный прогон.
    """
    from finengine.roles.scope import build_engine_scope

    _plan, enums = build_engine_scope("other", "non_financial", "ifrs_nf_ru")
    assert len(enums) > 100, f"полный прогон ужался до {len(enums)} — сужение зацепило умолчание"
