"""Промпт извлечения запрещает прогнозные КОЛОНКИ, а не только строки (#P3).

Раздел «только фактические значения» говорил про строки, разделы и таблицы, но
про шапку колонки — нет. Прогнозная колонка (`2026П`, `FY26E`, «план») читается
как обычный период, и её значение попадает в `values` наравне с фактическим.

Цена: с этого года цена ошибки выросла. Порог `_LATER_YEAR_SUPPORT_SHARE`
снижен до 35% реестра — значит прогнозные колонки, просочившиеся в `values`,
теперь легче сдвигают границу отчётного года на год вперёд.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать из `_actual_values_rules` строку про колонки
(любую из двух языковых) → падает соответствующая проверка ниже.
"""
from __future__ import annotations

from finengine.skills.finengine2_prompts import _actual_values_rules


def test_the_russian_rules_name_the_column_header() -> None:
    rules = _actual_values_rules("ru")
    assert "колонк" in rules
    assert "FY26E" in rules


def test_the_english_rules_name_the_column_header() -> None:
    rules = _actual_values_rules("en")
    assert "columns" in rules
    assert "FY26F" in rules
