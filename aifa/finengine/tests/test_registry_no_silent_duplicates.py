"""Один показатель — одно происхождение, иначе это два числа под одним именем (#163).

ЗАЧЕМ. Шесть enum'ов в нефинансовых ячейках заведены И в `extractable`, И в
`calculated`. Движок эмитит на такой показатель ДВА события, редьюсер таблицы
заземления (`calc.dataset_from_metrics`) оставляет ПОСЛЕДНЕЕ — расчётное, — а
свой `FREE_CASH_FLOW` движок считает по ИЗВЛЕЧЁННОМУ. Доказано исполнением:
при CAPEX(извлечён) = −85 000 и ASSET_PURCHASES = −100 000 движок вернул
`{'CAPEX': 100 000, 'FREE_CASH_FLOW': 35 000}` — в таблицу уехало одно число,
а производное посчитано от другого.

Задето не только графики: EBITDA и валовая прибыль лежат в карточке и в Excel,
и от них считаются коэффициенты.

ПОЧЕМУ ЗДЕСЬ СПИСОК, А НЕ ЗАПРЕТ. Правило приоритета — вопрос МЕТОДИКИ, а не
кода: «что компания раскрыла сама» против «что сопоставимо между компаниями»
(скорректированную EBITDA каждый считает по-своему). Решение за владельцем, оно
открыто в #163. Пока оно не принято, запрет уронил бы сюиту и заставил бы
принять решение впопыхах — худший способ его принимать.

Поэтому дубли ЗАФИКСИРОВАНЫ поимённо как известное расхождение. Смысл сторожа:
НОВЫЙ дубль завести молча нельзя, а когда #163 решится — список обязан
опустеть, и тест сам напомнит об этом.

⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ЛОВИТ (дописано ревизией 21.08.2026). Он смотрит на
СОСТАВ реестра и ничего не исполняет: какое из двух чисел уедет наружу и
какое из них возьмёт производный показатель, он не знает и знать не может.
Само ПОВЕДЕНИЕ двойной природы («извлечённое старше расчётного, наружу
уезжает одна строка») сторожат прогоном соседи —
`test_an_empty_line_does_not_cancel_the_formula.py`,
`test_registry_sums_are_counted_by_code.py`,
`test_the_fallback_engine_loses_dual_formulas.py`. Здесь только
бухгалтерия: молча новый дубль не заведёшь, и протухшую строку в списке
не оставишь.
"""
from __future__ import annotations

import pytest

from finengine.skills.metric_schema import PROFILE_IDS, registry_for_profile

# Снято прогоном 03.08.2026. Каждая строка — «показатель, у которого сегодня ДВА
# происхождения».
#
# 🔴 ПРАВИЛО «СПИСОК НЕ РАСТЁТ» ОТМЕНЕНО 20.08.2026, и вот чем. Оно написано
# 03.08, когда двойная природа была ДЕФЕКТОМ: движок слал на такой показатель
# два события, и редьюсер оставлял последнее. 19.08 это починено в
# `two_stage_extractor` (`_dual_nature_enums` + `deferred_dual`): извлечённое
# старше расчётного, наружу уезжает ОДНА строка. После этого двойная природа
# стала рабочим МЕХАНИЗМОМ — им закрывается «компоненты извлечены, итог пуст».
#
# Поэтому список теперь растёт ТОЛЬКО осознанно и только с причиной в
# комментарии рядом. Молчаливый дубль по-прежнему запрещён — тест ловит его.
#
# ⚠️ ЧЕГО ЭТО НЕ ОТМЕНЯЕТ: в `calc.dataset_from_metrics` предупреждение #163
# живо, и на путях, где два события всё-таки придут, последнее затрёт первое.
# Решение о приоритете за владельцем, задача #163 открыта.
KNOWN_DUPLICATES: dict[str, set[str]] = {
    "ifrs_nf_ru": {"CAPEX", "COMBINED_ASSET_PURCHASES", "DIVIDENDS", "EBITDA",
                   "GROSS_PROFIT", "RIGHT_OF_USE_ASSETS"},
    "ifrs_nf_en": {"CAPEX", "COMBINED_ASSET_PURCHASES", "DIVIDENDS", "EBITDA",
                   "GROSS_PROFIT", "RIGHT_OF_USE_ASSETS"},
    "us_gaap_nf_en": {"CAPEX", "COMBINED_ASSET_PURCHASES", "DIVIDENDS", "EBITDA",
                      "GROSS_PROFIT", "RIGHT_OF_USE_ASSETS"},
    "ind_as_nf_en": {"CAPEX", "COMBINED_ASSET_PURCHASES", "DIVIDENDS", "EBITDA",
                     "GROSS_PROFIT"},
    # SECURITIES_ASSETS добавлен 20.08.2026 ОСОЗНАННО, а не «завёлся». Ответ
    # владельцу на «складывать через калькулятор или в уме модели»: агрегат
    # ценных бумаг считает КОД из трёх категорий (FVPL + FVOCI + AC, репо
    # отдельной сущностью), а подсказка по-прежнему запрещает модели
    # досочинять итог. См. tests/test_registry_sums_are_counted_by_code.py.
    #
    # ⚠️ ТОЛЬКО В РУССКОЙ ЯЧЕЙКЕ. В `ifrs_bank_en` такую же формулу завели по
    # аналогии — и сняли 20.08.2026: там подсказка определяет показатель иначе
    # («marketable securities / short-term investments») и слагаемых не
    # называет, а согласие с разметкой 13 троек из 21 при одностороннем
    # завышении +23…49 % у Santander, HSBC и KBC. Состав английского агрегата —
    # вопрос владельцу, не аналогия.
    "ifrs_bank_ru": {"DIVIDENDS", "NET_COMMISSION_INCOME", "SECURITIES_ASSETS"},
    "ifrs_bank_en": {"DIVIDENDS", "NET_COMMISSION_INCOME"},
    # Ячейки РСБУ приехали из форк-базы 15.08.2026 со своими дублями — сняты
    # прогоном, не переписаны с соседей: у нефинансовой их два, у банковской
    # один, и DIVIDENDS среди них нет (в РСБУ он только извлекаемый).
    # CAPEX добавлен 20.08.2026: остальные четыре нефинансовые ячейки держат
    # его расчётным с самого начала, РСБУ была единственной без формулы.
    "rsbu_nf_ru": {"CAPEX", "EBITDA", "GROSS_PROFIT"},
    "rsbu_bank_ru": {"NET_COMMISSION_INCOME"},
    "ind_gaap_bank_en": {"NET_INTEREST_INCOME", "OPERATING_PROFIT_BEFORE_PROVISIONS",
                         "PROVISION_COVERAGE_RATIO"},
    "us_gaap_bank_en": set(),  # чистая ячейка — доказательство, что так МОЖНО
}


def _enums(cell: str) -> tuple[set[str], set[str]]:
    d = registry_for_profile(cell)
    ex = {m.get("enum") for m in d["extractable"]}
    ca = {row[0] for row in d["calculated"]}
    return {e for e in ex if e}, {e for e in ca if e}


def _cells() -> list[str]:
    return sorted(PROFILE_IDS)


def test_the_registry_folder_is_where_we_think_it_is():
    """Если реестр перестанет компилироваться, проверки не зеленеют на пустом."""
    assert _cells() and all(registry_for_profile(cell)["extractable"] for cell in _cells())


@pytest.mark.parametrize("cell", _cells())
def test_no_new_metric_gets_two_origins(cell):
    """НОВЫЙ дубль завести молча нельзя.

    Именно молча он и завёлся: ни один тест не смотрел на пересечение, и
    расхождение всплыло только состязательной проверкой — через полгода после
    того, как показатели начали расходиться в карточке.
    """
    ex, ca = _enums(cell)
    dup = ex & ca
    known = KNOWN_DUPLICATES.get(cell)
    assert known is not None, (
        f"ячейка «{cell}» не описана в KNOWN_DUPLICATES — заведите строку "
        f"осознанно: сейчас у неё дублей {sorted(dup)}")
    new = dup - known
    assert not new, (
        f"в ячейке «{cell}» завёлся НОВЫЙ показатель с двумя происхождениями: "
        f"{sorted(new)}. Он и извлекается, и считается — значит движок пришлёт "
        f"на него два события, а в таблицу заземления попадёт последнее (#163).")


@pytest.mark.parametrize("cell", _cells())
def test_the_known_list_shrinks_but_never_silently(cell):
    """Обратная сторона: починили — вычеркните.

    Список известных расхождений, который живёт своей жизнью, через полгода
    становится списком того, чего давно нет, и следующий читатель ему верит.
    """
    ex, ca = _enums(cell)
    stale = (KNOWN_DUPLICATES.get(cell) or set()) - (ex & ca)
    assert not stale, (
        f"в «{cell}» дублей {sorted(stale)} больше НЕТ — вычеркните их из "
        f"KNOWN_DUPLICATES, иначе список охраняет прошлое")


def test_a_clean_cell_proves_the_rule_is_achievable():
    """Противовес списку: хотя бы одна ячейка обязана быть чистой.

    Иначе «дубли есть везде» незаметно превращается в «так и надо».
    """
    clean = [c for c in _cells() if not (lambda p: p[0] & p[1])(_enums(c))]
    assert clean, "чистых ячеек не осталось — правило перестало быть достижимым"
