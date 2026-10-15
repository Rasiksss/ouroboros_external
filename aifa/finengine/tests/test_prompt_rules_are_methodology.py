"""Правила промпта — методика, а не литералы в коде.

ЗАЧЕМ ЭТОТ ФАЙЛ. До 09.08.2026 бо́льшая часть правил, реально доезжавших до
модели, была зашита строками прямо в `framework.build_prompt`: около 4 900 знаков
против 12 744 в элементах методики. У литерала нет идентификатора, а значит:
методолог не видит его в каталоге, пользователь не может его поправить, а личной
дельте не к чему прицепиться. Рычаг у владельца был только на видимую половину.

Эталоны `tests/golden/methodology/` доказывают, что перенос не изменил ни одного
промпта. Но они НЕ доказывают обратного — что рычаг появился: промпт остался бы
тем же и если бы литералы просто скопировали в элементы, а сборка по-прежнему
брала свои строки. Здесь проверяется именно следствие переноса.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в `build_prompt` любой литерал вместо
`m.text_of(...)` — соответствующий случай `test_editing_an_element_changes_the_prompt`
обязан покраснеть.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from finengine.skills.analysis import framework as F
from finengine.skills.analysis import methodology as md

OCR = (
    "===PAGE 1===\nОтчет о прибылях и убытках\nВыручка 100\n\n"
    "===PAGE 2===\nПримечание 5. Коэффициент достаточности капитала\nН1.0 12,5%\n\n"
)

# Каждый элемент каркаса промпта и конфигурация прогона, в которой он ОБЯЗАН
# появиться. Пары подобраны так, чтобы покрыть все ветки сборки: глубина,
# наличие таблицы заземления, наличие нормативов капитала, тип компании.
CASES = [
    ("prompt.hard_rules", {"company_type": "non_financial", "depth": "full"}),
    ("prompt.depth.full", {"company_type": "non_financial", "depth": "full"}),
    ("prompt.depth.quick", {"company_type": "non_financial", "depth": "quick"}),
    ("prompt.grounding.intro", {"company_type": "non_financial", "depth": "full",
                                "metrics_table": "SALES_REVENUE | Выручка | 100\n"}),
    ("prompt.grounding.rules", {"company_type": "non_financial", "depth": "full",
                                "metrics_table": "SALES_REVENUE | Выручка | 100\n"}),
    ("prompt.page_source", {"company_type": "non_financial", "depth": "full",
                            "metrics_table": "SALES_REVENUE | Выручка | 100\n"}),
    ("prompt.capital.facts", {"company_type": "bank", "depth": "full",
                              "capital_facts": "Н1.0: 12,5 / 11,8"}),
    ("prompt.capital.note", {"company_type": "bank", "depth": "full"}),
]


def _prompt(**kw) -> str:
    # Таблица заземления в базе набора, а не только в трёх случаях (#238):
    # промпта БЕЗ неё больше не существует — `build_prompt` отказывается его
    # собирать. Случаи, которым таблица важна по смыслу, задают свою.
    base = {"company_name": "Тест", "standard": "ifrs", "ocr_text": OCR,
            "metrics_table": "SALES_REVENUE | Выручка | 100\n"}
    return F.build_prompt(**{**base, **kw})


@pytest.mark.parametrize(("eid", "kw"), CASES, ids=[c[0] for c in CASES])
def test_element_text_reaches_the_prompt(eid, kw):
    """Элемент не сирота: его текст есть в собранном промпте.

    Элемент, который никто не читает, — это методика, которую владелец правит
    впустую. Такое у нас уже случалось с навыками и с плагином, и стоило дня.
    """
    text = md.base().text_of(eid)
    assert text.strip() and text.strip() in _prompt(**kw), (
        f"текст элемента «{eid}» не доехал до промпта — правка методики "
        f"в этом месте ничего не изменит"
    )


@pytest.mark.parametrize(("eid", "kw"), CASES, ids=[c[0] for c in CASES])
def test_editing_an_element_changes_the_prompt(eid, kw):
    """ГЛАВНАЯ проверка: правка элемента меняет промпт.

    Именно это отличает «текст скопировали в методику» от «сборка читает
    методику». Эталоны байт-в-байт этого не ловят: они одинаково зелены и
    когда рычаг работает, и когда рядом с элементом лежит забытый литерал.
    """
    marker = "ЯБЛОКО-СТОРОЖ-09-08"
    m = md.base()
    edited = replace(m, elements={
        **m.elements, eid: replace(m.element(eid), text=marker + "\n")})

    before = _prompt(**kw)
    after = _prompt(**kw, methodology=edited)

    assert marker in after, (
        f"правка «{eid}» не доехала до промпта — сборка берёт текст мимо методики"
    )
    assert after != before


def test_external_rules_come_from_methodology():
    """Внешний блок собирается отдельной функцией — проверяется отдельно.

    Имя раздела в правилах подставляется из константы `EXTERNAL_SECTION_TITLE`:
    то же имя читает разбор ответа (`parse.py`), и две копии разъехались бы при
    первом переименовании.
    """
    from finengine.skills.analysis.external import ExternalFact

    fact = ExternalFact(text="Пожар на складе", source="vedomosti.ru",
                        url="https://example.com/x", date="2024-05-01")
    block = F.external_block([fact])
    assert F.EXTERNAL_SECTION_TITLE in block, "имя раздела не подставилось"
    assert "{{EXTERNAL_SECTION_TITLE}}" not in block, "подстановка не сработала"

    m = md.base()
    edited = replace(m, elements={
        **m.elements,
        "prompt.external.rules": replace(m.element("prompt.external.rules"),
                                         text="ГРУША-СТОРОЖ\n")})
    assert "ГРУША-СТОРОЖ" in F.external_block([fact], edited)


