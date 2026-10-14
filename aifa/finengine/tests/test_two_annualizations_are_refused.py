"""Два приведения к году в одной записи реестра — отказ, а не ×16.

ДВА РАЗНЫХ МЕХАНИЗМА, И ОБА ПОДНИМАЮТ ДО ГОДА. Их легко перепутать, потому что
называются они почти одинаково:

  • `ANNUAL(X)` В ФОРМУЛЕ поднимает поток В ЗНАМЕНАТЕЛЕ — «чистый долг /
    ANNUAL(EBITDA)», оборачиваемости через `DAYS_IN_PERIOD`. Раскрывается в
    `formula_for_period` ещё до разбора выражения;
  • признак `"annualize": true` В РЕЕСТРЕ поднимает готовое число ПРИ ВЫДАЧЕ —
    там, где поток стоит в ЧИСЛИТЕЛЕ (ROE, ROA, NIM, CoR).

Порознь каждый прав. Вместе на квартальном отчёте они дают ×16 вместо ×4.

ПОЧЕМУ СТОРОЖ НУЖЕН СЕГОДНЯ, КОГДА ПАРЫ НЕТ НИ В ОДНОЙ ЯЧЕЙКЕ. Реестр — это
данные (#153): он правится ТЕКСТОМ, аналитиком, без программиста и без сборки
образа. Аналитик, дописавший `ANNUaL(...)` показателю с признаком, не увидел бы
ни ошибки, ни предупреждения — он увидел бы правдоподобное число. Ровно тот
класс молчаливых поломок, ради которого в этом модуле стоит `extra=forbid` и
строгая проверка конверта.

Отказ поднимается на ЗАГРУЗКЕ ячейки и называет файл, показатель и формулу —
этого хватает, чтобы починить за минуту.

ПРОВЕРОК ДВЕ, И ОНИ НЕ ДУБЛИРУЮТ ДРУГ ДРУГА. Строка реестра видит только СВОЮ
формулу — соседних строк ячейки у неё нет, поэтому `CalcDef` закрывает лишь
случай «признак и `ANNUAL(...)` в одном тексте». Длина периода приезжает и ПО
ЗАВИСИМОСТИ: у денежного цикла в формуле её нет, а у всех трёх его слагаемых
есть. Этот случай ловит обход ячейки целиком
(`_refuse_annualize_on_top_of_period_length`), и стоять он может только там, где
видны все строки — в разборе реестра.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 21.08.2026, каждая покраснела):
  • обезвредить условие в `CalcDef._one_annualization_not_two` (`if False and
    ...`) → падает `test_two_annualizations_in_one_row_are_refused`,
    остальные сторожа ветки остаются зелёными;
  • вернуть проверке поиск ПОДСТРОКИ (`"ANNUAL(" in formula.replace(" ", "")`)
    → падают три случая
    `test_the_refusal_matches_what_the_engine_actually_expands`:
    `ANNUAL\t(EBITDA)`, `SEMIANNUAL(...)`, `MY_DAYS_IN_PERIOD`;
  • не звать обход зависимостей из `registry_from_data` → падают
    `test_a_composite_metric_inherits_the_period_length` и
    `test_the_walk_goes_deeper_than_one_step`;
  • отказывать на ЛЮБОЙ расчётной зависимости → падают
    `test_a_clean_chain_of_dependencies_is_allowed` и
    `test_no_shipped_cell_carries_both` (обход стал шире, чем надо);
  • не класть зависимость обратно в очередь (обход на один шаг) → падает
    `test_the_walk_goes_deeper_than_one_step`.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from finengine.skills.bulk_extractor.formula_engine import formula_for_period
from finengine.skills.metric_schema import (
    CalcDef,
    PROFILE_IDS,
    RegistryError,
    compiled_registry_for_profile,
    registry_for_profile,
    registry_from_data,
)


def test_two_annualizations_in_one_row_are_refused():
    """Признак вместе с обёрткой в формуле — отказ схемы."""
    with pytest.raises(ValidationError):
        CalcDef(enum="ROE", formula="NET_INCOME / ANNUAL(AVG (TOTAL_EQUITY))",
                annualize=True)
    with pytest.raises(ValidationError):
        CalcDef(enum="DSO", formula="DAYS_IN_PERIOD * RECEIVABLES / REVENUE",
                annualize=True)


# Формулы, на которых проверка и движок расходились. Отбор не выдуманный: это
# ровно те написания, где `"ANNUAL(" in formula.replace(" ", "")` отвечал не то,
# что делает `formula_for_period` (замер 21.08.2026).
_SPELLINGS = (
    "NET_DEBT / ANNUAL(EBITDA)",
    "NET_DEBT / ANNUAL (EBITDA)",
    "NET_DEBT / ANNUAL\t(EBITDA)",
    "DAYS_IN_PERIOD * RECEIVABLES / REVENUE",
    "X / SEMIANNUAL(EBITDA)",
    "MY_DAYS_IN_PERIOD / X",
    "NET_INCOME / AVG (TOTAL_EQUITY)",
)


@pytest.mark.parametrize("formula", _SPELLINGS)
def test_the_refusal_matches_what_the_engine_actually_expands(formula):
    """Проверка отказывает РОВНО ТАМ, где движок раскрывает длину периода.

    Проверка искала подстроку, движок ищет по границе слова — и врали они в обе
    стороны (замер 21.08.2026):

      • ПРОПУСК: `ANNUAL\\t(EBITDA)` с признаком проходил, потому что
        `.replace(" ", "")` табуляцию не снимает. Движок эту формулу раскрывает
        в `NET_DEBT / ((EBITDA) * 12/3)` — то есть ×16 на квартале, ровно то,
        ради чего проверка написана;
      • ОТКАЗ: `SEMIANNUAL(EBITDA)` и `MY_DAYS_IN_PERIOD` отвергались, хотя
        движок их не трогает — своё имя показателя закрывало ячейку целиком.

    Сверяется не список написаний, а СОГЛАСИЕ ДВУХ ОТВЕТОВ: спорить о том, чей
    разбор правильный, тут не о чем — правильный тот, по которому считают.
    """
    engine_raises_to_year = formula_for_period(formula, "Q1 2025") != formula
    try:
        CalcDef(enum="X", formula=formula, annualize=True)
        schema_refuses = False
    except ValidationError:
        schema_refuses = True
    assert schema_refuses == engine_raises_to_year, (
        f"на формуле «{formula}» схема "
        f"{'отказывает' if schema_refuses else 'пропускает'}, а движок "
        f"{'раскрывает длину периода' if engine_raises_to_year else 'её не трогает'}"
        f" — «{formula_for_period(formula, 'Q1 2025')}»")


def test_a_composite_metric_inherits_the_period_length(tmp_path):
    """Длина периода приезжает ПО ЗАВИСИМОСТИ, и это тоже отказ.

    `CASH_CONVERSION_CYCLE = дебиторка + запасы − кредиторка`: в своей формуле
    ни `ANNUAL(...)`, ни `DAYS_IN_PERIOD` нет, а у всех трёх слагаемых есть.
    Строчная проверка такую запись пропускает — она видит только свою формулу.

    ЗАМЕР (ifrs_nf_ru, Q1, `DAYS_IN_PERIOD` = 91, входы 1000/800/250/200/100):
    цикл 34,125 дня превращается в 136,5 — в полтора раза длиннее самого
    квартала, и ни слова об ошибке.
    """
    raw = compiled_registry_for_profile("ifrs_nf_ru").to_effective_data()
    for row in raw["calculated"]:
        if row.get("enum") == "CASH_CONVERSION_CYCLE":
            row["annualize"] = True
    with pytest.raises(RegistryError) as exc:
        registry_from_data("ifrs_nf_ru", raw, source=str(tmp_path / "ifrs_nf_ru.json"))
    message = str(exc.value)
    # Ошибка обязана назвать ПУТЬ: сам показатель чист, и без цепочки аналитик
    # ищет `DAYS_IN_PERIOD` в формуле, где его нет.
    assert "CASH_CONVERSION_CYCLE" in message and "TURNOVER" in message, message
    assert "→" in message, f"в отказе нет пути до зависимости: {message}"


# Цепочка ЛЕГАЛЬНАЯ и цепочка с длиной периода на ВТОРОМ шаге. Реестр здесь
# собран руками, и иначе нельзя: ни одна из десяти поставляемых ячеек такой
# записи не содержит — за тем и заведён запрет. Выдумка тут вход, а не ответ.
_CLEAN_CHAIN = [
    {"enum": "BASE", "formula": "REVENUE - COSTS"},
    {"enum": "MIDDLE", "formula": "BASE / 2"},
    {"enum": "TOP", "formula": "MIDDLE / EQUITY", "annualize": True},
]
_CHAIN_WITH_PERIOD_LENGTH = [
    {"enum": "BASE", "formula": "DAYS_IN_PERIOD / (COSTS / AVG (PAYABLES))"},
    {"enum": "MIDDLE", "formula": "BASE / 2"},
    {"enum": "TOP", "formula": "MIDDLE / EQUITY", "annualize": True},
]


def test_the_walk_goes_deeper_than_one_step():
    """Длина периода через ДВА шага зависимостей — тоже отказ."""
    with pytest.raises(RegistryError) as exc:
        registry_from_data("ifrs_nf_ru",
                           {"extractable": [], "calculated": _CHAIN_WITH_PERIOD_LENGTH},
                           source="<цепочка на два шага>")
    assert "TOP → MIDDLE → BASE" in str(exc.value), str(exc.value)


def test_a_clean_chain_of_dependencies_is_allowed():
    """Отрицательный контроль: обход не отказывает на любой расчётной зависимости.

    Запрет, срабатывающий на самом факте зависимости от расчётного показателя,
    закрыл бы `EBITDA_MARGIN`, `FREE_CASH_FLOW_DEBT` и ещё полтора десятка живых
    записей — а предыдущий сторож этого не заметил бы, он проверяет только отказ.
    """
    registry = registry_from_data("ifrs_nf_ru",
                                  {"extractable": [], "calculated": _CLEAN_CHAIN},
                                  source="<чистая цепочка>")
    assert registry["calculated_meta"]["TOP"]["annualize"] is True


def test_each_mechanism_alone_is_allowed():
    """Отрицательный контроль: порознь оба механизма законны.

    Валидатор, запрещающий `ANNUAL(...)` вовсе, закрыл бы настоящие записи
    реестра (чистый долг / EBITDA, оборачиваемости), а сторож выше этого не
    заметил бы — он проверяет только отказ.
    """
    CalcDef(enum="NET_DEBT_TO_EBITDA", formula="NET_DEBT / ANNUAL(EBITDA)")
    CalcDef(enum="ROE", formula="NET_INCOME / AVG (TOTAL_EQUITY)", annualize=True)


def test_no_shipped_cell_carries_both():
    """Ни одна из десяти поставляемых ячеек пары не содержит.

    Без этого сторож выше ловил бы выдумку: запрет, которому противоречит
    поставка, роняет прогон на первом же документе, а не на правке аналитика.

    Отрицательный контроль сразу к обеим проверкам — и строчной, и обходу
    зависимостей: 40 записей с признаком в восьми ячейках, ни у одной длины
    периода нет ни в своей формуле, ни в транзитивных зависимостях (замер
    21.08.2026). Обход, отказывающий шире, чем надо, красит этот сторож.
    """
    assert len(PROFILE_IDS) == 10, f"ячеек стало {len(PROFILE_IDS)}, а не десять — сверить состав"
    for profile_id in PROFILE_IDS:
        registry_for_profile(profile_id)
