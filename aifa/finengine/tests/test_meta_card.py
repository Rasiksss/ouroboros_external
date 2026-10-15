"""Метакарта документа: код считает, модель называет (#189).

ЗАЧЕМ ЭТОТ ФАЙЛ. Карта решает две задачи владельца: агент перестаёт открывать
не тот документ и перестаёт гнать полный реестр по пресс-релизу. Обе ломаются
одинаково — молча: карта не снялась, поля пустые, агент ведёт себя как раньше.

Здесь проверяется граница код↔модель, ради которой модуль и разделён надвое:
счётные величины не спрашиваются у модели, а решения не принимаются кодом.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: заменить в `_clean` возврат «прочее» на подгонку к
ближайшему слову словаря — `test_unknown_kind_is_not_guessed` обязан покраснеть.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.skills import meta_card as mc

OCR = ("===PAGE 1===\nОтчет о финансовом положении\nВыручка 100\n\n"
       "===PAGE 2===\nПримечание 1\n\n===PAGE 3===\nПрочее\n")


def test_measure_counts_and_does_not_judge():
    got = mc.measure(OCR)
    assert got["chars"] == len(OCR)
    assert got["pages_marked"] == 3
    assert got["has_page_markers"] is True
    # Никаких оценочных полей: что это за документ — не дело замера.
    assert set(got) == {"chars", "lines", "pages_marked", "has_page_markers"}


def test_missing_page_markers_are_stated_not_hidden():
    """Без разметки анализ не сможет дать ссылку «PDF стр. N» — это надо знать."""
    got = mc.measure("просто текст без страниц")
    assert got["has_page_markers"] is False
    assert got["pages_marked"] == 0


def test_model_answer_is_parsed(monkeypatch):
    async def fake(prompt, model=None):
        return ('{"doc_kind": "отчётность", "issuer": "ФосАгро", '
                '"issuer_aliases": ["PhosAgro", "ПАО ФосАгро"], '
                '"period": "2024", "summary": "годовая отчётность по МСФО"}')
    monkeypatch.setattr("finengine.llm.openrouter.complete", fake)
    got = asyncio.run(mc.describe(OCR))
    assert got["doc_kind"] == "отчётность"
    assert got["issuer"] == "ФосАгро"
    assert "PhosAgro" in got["issuer_aliases"]
    assert got["period"] == "2024"


def test_answer_wrapped_in_a_code_block_is_still_parsed(monkeypatch):
    async def fake(prompt, model=None):
        return '```json\n{"doc_kind": "пресс-релиз", "issuer": "X"}\n```'
    monkeypatch.setattr("finengine.llm.openrouter.complete", fake)
    assert asyncio.run(mc.describe(OCR))["doc_kind"] == "пресс-релиз"


def test_unknown_kind_is_not_guessed(monkeypatch):
    """Вид вне словаря НЕ подгоняем к ближайшему: подгонка — решение кодом."""
    async def fake(prompt, model=None):
        return '{"doc_kind": "квартальная отчётность эмитента", "issuer": "X"}'
    monkeypatch.setattr("finengine.llm.openrouter.complete", fake)
    got = asyncio.run(mc.describe(OCR))
    assert got["doc_kind"] == "прочее", "код угадал вид вместо модели"
    assert got["doc_kind_raw"] == "квартальная отчётность эмитента", (
        "исходный ответ модели потерян — разобраться постфактум будет нечем"
    )


def test_model_failure_does_not_lose_the_measurements(monkeypatch):
    """Модель не ответила — объём и страницы всё равно записаны, и сказано почему."""
    async def boom(prompt, model=None):
        raise RuntimeError("прокси не пропустил модель")
    monkeypatch.setattr("finengine.llm.openrouter.complete", boom)
    card = asyncio.run(mc.build(OCR))
    assert card["pages_marked"] == 3, "замеры потеряны из-за неудачи модели"
    assert "прокси не пропустил" in card["error"]
    assert "doc_kind" not in card, "вид документа выдуман при неудаче"


def test_empty_text_is_refused_without_calling_the_model(monkeypatch):
    def must_not_run(*a, **kw):
        pytest.fail("модель вызвана на пустом тексте")
    monkeypatch.setattr("finengine.llm.openrouter.complete", must_not_run)
    assert "пуст" in asyncio.run(mc.describe("   "))["error"]


def test_line_for_the_model_names_what_matters():
    card = {**mc.measure(OCR), "doc_kind": "отчётность", "issuer": "ФосАгро",
            "period": "2024", "summary": "годовая отчётность по МСФО"}
    line = mc.as_line(card, filename="phosagro.pdf")
    for must in ("phosagro.pdf", "отчётность", "ФосАгро", "2024", "стр. 1–3"):
        assert must in line, f"в строке для модели нет «{must}»: {line}"


def test_line_says_plainly_when_there_is_no_card():
    assert "карта не снята" in mc.as_line({}, filename="x.pdf")
