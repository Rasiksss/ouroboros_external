"""Разрезы у банка: стадия запускается и ищет банковские показатели (#252).

ЗАЧЕМ ЭТОТ ФАЙЛ. Владелец трижды подряд получал пустые сегменты на банках, а в
последнем прогоне Ак Барса — две строки с пометкой `main_total_fallback`. Разбор
16.08 нашёл ДВА дефекта, и каждый по отдельности обнуляет результат.

ДЕФЕКТ 1 — ОТКАЗ ОБСЛУЖИВАТЬ. В `_extract_segment_breakdown_full_context` первой
строкой тела стояло:

    if effective_type == "bank":
        return

Стадия молча выходила для любого банка. Из-за него правка #252 («включить
сегменты банкам») не дала НИЧЕГО: флаг доезжал до вызова, а исполнитель двумя
уровнями ниже отказывался. Это болезнь «два места» в чистом виде — включатель и
исполнитель разошлись, и сторож на флаг этого не видел.

ДЕФЕКТ 2 — СПРАШИВАЛИ НЕ ТО. Область поиска в разрезах была одна на всех:
`{SALES_REVENUE, OPERATING_PROFIT, NET_INCOME, INTEREST_PAID, EBITDA}`. Первых
трёх и EBITDA в ячейке `ifrs_bank_ru` НЕТ ВООБЩЕ — у банка не бывает ни выручки,
ни операционной прибыли. Оставались двое, и ровно они приехали фоллбэком:

    было (банк)  : INTEREST_PAID, NET_INCOME                         — 2
    стало (банк) : INTEREST_INCOME_ER, FEE_AND_COMMISSION_INCOME,
                   NET_INTEREST_INCOME, OPERATING_INCOME_BANKS,
                   NET_INCOME                                        — 5
    нефинансовая : без изменений                                     — 5

Состав собран по строкам сегментной записки банка (МСФО (IFRS) 8): процентные
доходы, комиссионные доходы, ЧПД, совокупные операционные доходы, результат.
«Сегментная прибыль до налогообложения» сознательно НЕ включена: точного enum
под неё в ячейке нет, а `NET_INCOME` — величина после налога. Промах лучше
ложного значения — это правило записано в самом промпте извлечения.

⚠️ ЭТО ВРЕМЕННОЕ МЕСТО ДЛЯ СОСТАВА. Списки показателей — данные, а не код; их
место в реестре ячейки (#224/#250), рядом с остальными свойствами показателя.
Пока состав живёт в коде, его правка требует программиста — ровно то, от чего мы
уходим. Переносить вместе с реестром, не отдельной задачей.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть ранний `return` для банка — покраснеет
`test_the_bank_is_not_refused_service`; вернуть общий список — покраснеет
`test_the_bank_is_asked_for_bank_lines`.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

from finengine.skills.bulk_extractor import (
    _extract_segment_breakdown_full_context,
    _segment_metric_scope,
)
from finengine.skills.metric_schema import registry_for_profile


def _cell_metrics(cell: str) -> list[dict]:
    return [{"enum": m["enum"]} for m in registry_for_profile(cell)["extractable"]]


def test_the_bank_is_not_refused_service():
    """ГЛАВНОЕ: стадия разрезов больше не выходит по одному лишь типу компании.

    Проверяем ТЕЛО функции, а не поведение обёртки: именно ранний `return`
    обесценил правку #252, и именно он не виден никакому сторожу на флаг.
    """
    # Разбор дерева, а не поиск по тексту: описание функции цитирует снятый код,
    # и текстовый сторож ловил бы собственную цитату вместо самого кода.
    src = textwrap.dedent(inspect.getsource(_extract_segment_breakdown_full_context))
    fn = ast.parse(src).body[0]
    for node in fn.body:
        if not isinstance(node, ast.If):
            continue
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        literals = {n.value for n in ast.walk(node.test) if isinstance(n, ast.Constant)}
        bare_return = any(
            isinstance(st, ast.Return) and st.value is None for st in node.body
        )
        assert not ("effective_type" in names and "bank" in literals and bare_return), (
            "в начале стадии разрезов снова стоит выход по типу компании — "
            "банкам сегменты не достанутся, сколько бы флагов их ни включало")


def test_the_bank_is_asked_for_bank_lines():
    """Банку задают банковские строки, а не выручку с EBITDA."""
    scope = {m["enum"] for m in _segment_metric_scope(_cell_metrics("ifrs_bank_ru"), "ifrs_ru", "bank")}
    assert "INTEREST_INCOME_ER" in scope and "FEE_AND_COMMISSION_INCOME" in scope, (
        f"в области поиска банка нет процентных или комиссионных доходов: {sorted(scope)}")
    assert len(scope) >= 4, (
        f"область поиска банка схлопнулась до {len(scope)} показателей: {sorted(scope)}")


def test_the_old_scope_was_almost_empty_for_a_bank():
    """Замер, ради которого правка делалась: общий список банку почти пуст.

    Не «плохо подобран», а физически отсутствует: выручки, операционной прибыли
    и EBITDA в ячейке банка нет как записей реестра.
    """
    bank_enums = {m["enum"] for m in _cell_metrics("ifrs_bank_ru")}
    for absent in ("SALES_REVENUE", "OPERATING_PROFIT", "EBITDA"):
        assert absent not in bank_enums, (
            f"{absent} появился в банковской ячейке — пересмотреть состав разрезов")


def test_the_nonfinancial_scope_is_untouched():
    """Отрицательный контроль: нефинансовой компании ничего не поменяли."""
    scope = {m["enum"] for m in _segment_metric_scope(_cell_metrics("ifrs_nf_ru"), "ifrs_ru", "nonfinancial")}
    assert scope == {"SALES_REVENUE", "OPERATING_PROFIT", "NET_INCOME", "INTEREST_PAID", "EBITDA"}, (
        f"область поиска нефинансовой компании изменилась: {sorted(scope)}")


def test_us_gaap_still_wins_over_the_default():
    """Ветка US GAAP не перекрыта банковской — порядок условий важен."""
    metrics = [{"enum": e} for e in ("SALES_REVENUE", "GROSS_PROFIT", "NET_INCOME")]
    scope = {m["enum"] for m in _segment_metric_scope(metrics, "us_gaap_en", "nonfinancial")}
    assert "GROSS_PROFIT" in scope, "US GAAP потерял валовую прибыль"


def test_company_type_is_optional():
    """Старые вызовы без типа компании не падают — сигнатура совместима."""
    scope = _segment_metric_scope([{"enum": "NET_INCOME"}], "ifrs_ru")
    assert [m["enum"] for m in scope] == ["NET_INCOME"]


# ════════════════════════════════════════════════════════════════════════════
# ДОПИСАНО ПО #257: те же два дефекта приехали заново с переносом Finengine.
#
# Двухстадийная схема — их код, и в нём ОБА запрета на месте: ранний `return`
# в стадии разрезов и `and effective_type != "bank"` в условии, которое до этой
# стадии допускает. Пара запретов и объясняла замер «30 сегментов против 0»:
# разница была не в схеме извлечения, а в этих двух строках.
#
# Сторожи ниже держат перенесённую схему тем же способом, что и наш движок, —
# разбором дерева. Следующий перенос от них снова принесёт эти строки, и
# краснеть должно ЗДЕСЬ, а не на прогоне владельца.
# ════════════════════════════════════════════════════════════════════════════


def test_the_ported_schema_does_not_refuse_the_bank_either():
    """В перенесённой стадии разрезов нет выхода по типу компании."""
    from finengine.skills.two_stage_extractor import (
        _extract_segment_breakdown_two_stage,
    )

    src = textwrap.dedent(inspect.getsource(_extract_segment_breakdown_two_stage))
    fn = ast.parse(src).body[0]
    for node in fn.body:
        if not isinstance(node, ast.If):
            continue
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        literals = {n.value for n in ast.walk(node.test) if isinstance(n, ast.Constant)}
        bare_return = any(
            isinstance(st, ast.Return) and st.value is None for st in node.body
        )
        assert not ("effective_type" in names and "bank" in literals and bare_return), (
            "в перенесённой стадии разрезов снова стоит выход по типу компании — "
            "банкам сегменты не достанутся, и замер A/B опять сравнит не схемы, "
            "а наличие этой строки")


def test_the_ported_gate_lets_the_bank_through():
    """Условие допуска до стадии не отсекает банк.

    Второй запрет: `segments_supported = ... and effective_type != "bank" ...`.
    Он стоит уровнем выше и делает первый недостижимым — снимать нужно оба,
    иначе снятие одного ничего не меняет.
    """
    import finengine.skills.two_stage_extractor as ts

    tree = ast.parse(inspect.getsource(ts))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "segments_supported"
            for t in node.targets
        ):
            continue
        names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
        literals = {n.value for n in ast.walk(node.value) if isinstance(n, ast.Constant)}
        assert not ("effective_type" in names and "bank" in literals), (
            "условие допуска до сегментов снова отсекает банк — до стадии "
            "разрезов дело не дойдёт, сколько её ни чини")


def test_the_ported_schema_asks_the_bank_for_bank_lines():
    """Тип компании доезжает до набора показателей разрезов.

    Без него банку уедет набор нефинансовой компании (выручка, EBITDA), и в
    ячейке `ifrs_bank_ru` он не совпадёт почти ни с чем — ровно дефект 2 выше.
    """
    from finengine.skills.two_stage_extractor import (
        _extract_segment_breakdown_two_stage,
    )

    src = textwrap.dedent(inspect.getsource(_extract_segment_breakdown_two_stage))
    calls = [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_segment_metric_scope"
    ]
    assert calls, "перенесённая схема вообще не запрашивает область поиска разрезов"
    assert any(len(c.args) >= 3 for c in calls), (
        "в область поиска разрезов не передан тип компании — банк получит "
        "набор нефинансовой компании")
