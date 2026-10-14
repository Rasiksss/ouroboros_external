"""Двухстадийное извлечение не теряет реестр ячейки и синонимы (#257).

ЗАЧЕМ ЭТОТ ФАЙЛ. Перенесённая схема Finengine — кандидат заменить наш путь
извлечения. Но у нас поверх неё сделаны две вещи, которых в ней нет, и обе
пропали бы МОЛЧА, без единой ошибки:

  #147  реестр ЯЧЕЙКИ доезжает до промпта. Боевой путь передаёт весь словарь
        `registry_for_profile` параметром `registry_override`
        (`station-skills/aifa-extract/scripts/extract.py:192`). Ради этого
        файла сделана и работа #224/#250 — восемь ячеек, 600 из 600 записей
        справочника. Не дойди параметр — справочник просто не читается, а
        правки аналитика перестают влиять на извлечение;

  01.08 СИНОНИМЫ ОБЪЕДИНЯЮТСЯ, а не замещаются. Файлы ячеек команда правила
        под свою отчётность, и подсказки «где искать» там лучше общих, но
        список синонимов в них ТОЛЬКО РУССКИЙ. Переход «в лоб» терял 169
        латинских синонимов у всех 78 показателей `ifrs_bank_ru` и 192 у 75 в
        `ifrs_nf_ru` — Total assets, PP&E, Long-term borrowings. Русские отчёты
        по МСФО часто двуязычные.

ЧТО БЫЛО НА САМОМ ДЕЛЕ (важно для точности, я сначала сказал иначе). Механизм
`registry_override` в перенесённом коде ЕСТЬ: внутренняя функция его принимает и
применяет. Не было ПРОБРОСА через публичный вход — параметр не объявлен и не
передан. Я сперва заявил, что механизма нет вовсе; вывод был сделан по грепу,
в котором самого имени `registry_override` не было. Проверка полным грепом это
исправила.

ПОЧЕМУ ЭТО НЕ ПРИДИРКА К СТИЛЮ. Без этих двух вещей замер сравнивал бы «наш
движок С реестром ячейки» против «их движка БЕЗ него» — и ответил бы не на тот
вопрос, какой задан. Ровно та ловушка, из-за которой перед замером выяснялись
и модели.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать `registry_override` из сигнатуры публичного
входа двухстадийного — покраснеет `test_the_public_entry_passes_the_cell_registry`.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

from finengine.skills.bulk_extractor import extract_stream_finengine_dev
from finengine.skills.two_stage_extractor import (
    extract_stream_finengine_dev_two_stage,
)


def test_the_public_entry_passes_the_cell_registry():
    """ГЛАВНОЕ: публичный вход объявляет параметр И передаёт его внутрь."""
    params = inspect.signature(extract_stream_finengine_dev_two_stage).parameters
    assert "registry_override" in params, (
        "публичный вход двухстадийного не принимает реестр ячейки — справочник "
        "восьми ячеек (#224/#250) не будет прочитан, правки аналитика молча "
        "перестанут влиять на извлечение")
    src = textwrap.dedent(inspect.getsource(extract_stream_finengine_dev_two_stage))
    call = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call))
    assert any(kw.arg == "registry_override" for kw in call.keywords), (
        "параметр объявлен, но внутрь не передан — тихий холостой ход")


def test_no_parameter_of_ours_is_lost():
    """Ни один параметр нашего входа не потерян в двухстадийном.

    Сравниваем ИМЕНА параметров двух входов: перенос обязан быть заменой, а не
    сужением. Лишние у них допустимы — это их собственные ручки.
    """
    ours = set(inspect.signature(extract_stream_finengine_dev).parameters)
    theirs = set(inspect.signature(extract_stream_finengine_dev_two_stage).parameters)
    lost = sorted(ours - theirs)
    assert not lost, (
        f"двухстадийный не принимает то, что принимает наш путь: {lost}. "
        "Переключение потеряет эти возможности без единой ошибки")


def test_aliases_are_merged_not_replaced():
    """Синонимы ячейки ОБЪЕДИНЯЮТСЯ с общим реестром, а не замещают его.

    Замер 01.08: замещение «в лоб» стоило 169 латинских синонимов у 78
    показателей банка и 192 у 75 показателей нефинансовой компании.
    """
    import finengine.skills.two_stage_extractor as ts

    body = inspect.getsource(ts)
    assert "_merge_aliases_from_base" in body, (
        "двухстадийный берёт синонимы ячейки как есть — латинские синонимы "
        "общего реестра теряются, и двуязычные отчёты по МСФО перестают "
        "узнаваться по английским заголовкам таблиц")


def test_the_live_path_still_hands_the_registry_over():
    """Отрицательный контроль: боевой путь по-прежнему передаёт реестр.

    Сторож проверяет ОБЕ стороны стыка. Пропади передача на нашей стороне —
    параметр в двухстадийном останется, но кормить его будет нечем.

    ЗАКРЫТО (Срез 13.5): станционный `extract.py` больше не вызывает
    `two_stage_extractor` напрямую — живой путь идёт через graph-атомы
    `locate`/`read.value` (`graph_atoms._locate_batch_real`/
    `_read_value_batch_real`), которые берут реестр через
    `registry_for_passport(passport)` и передают его дальше как `metrics=`
    (см. `tests/test_cell_registry_reaches_prompt.py`, тот же инвариант,
    тот же приём проверки).
    """
    import inspect

    from finengine.skills.atoms import graph_atoms

    for real_batch_fn in (graph_atoms._locate_batch_real, graph_atoms._read_value_batch_real):
        src = inspect.getsource(real_batch_fn)
        assert "registry_for_passport(" in src, (
            f"{real_batch_fn.__name__}: боевой путь перестал передавать "
            "реестр ячейки в движок")
