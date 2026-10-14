"""Книга роли и таблица на экране отвечают на «как посчитано» ОДИНАКОВО.

ЦЕНА (Б5, состязательная проверка 20.08.2026). Подпись расчёта печатают два
места, и они разошлись: `roles/formatter.py` выводил сырые коды реестра и сырой
float (`= OPERATING_PROFIT + DEPRECIATION_AMORTIZATION; входы: … = 28355.0`), а
фронт переводил имена и форматировал число (`Операционная прибыль = 28 355`).
Аналитик, сверявший скачанную книгу с экраном, получал два разных ответа на
один вопрос — известный класс дефектов этого репозитория «два места для одного
знания».

ОДНОГО КОДА НА ДВА ЯЗЫКА НЕ БЫВАЕТ, поэтому один — РАЗМЕТКА. Правило и его
случаи лежат в `tests/fixtures/calculation_signature.json`, и её читают оба
сторожа: этот и `web/src/components/workspace/panes/calculation-hint.test.ts`.
Разошёлся любой отрисовщик — краснеет его сторож, и оба смотрят в один файл.

ПРОВЕРКА СТОРОЖА ОТКАТОМ (проделана 20.08): вернуть в `_calculated_source`
печать сырого `item["value"]` вместо `_ru_number` — тест краснеет и показывает
разницу; снять перевод формулы — краснеет тоже.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from finengine.roles.formatter import _calculated_source

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "calculation_signature.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_fixture_has_cases_to_check():
    """Пустая разметка озеленила бы сторож молча."""
    assert len(DATA["cases"]) >= 5
    assert DATA["names"]


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda c: c["name"])
def test_role_workbook_renders_the_signature_by_the_shared_rule(case: dict):
    assert _calculated_source(case["row"], DATA["names"]) == case["expect"]
