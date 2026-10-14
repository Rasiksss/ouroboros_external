"""Оглавление ячейки совпадает с реестром движка — побайтово.

ЗАЧЕМ ОГЛАВЛЕНИЕ (решение владельца 31.08.2026). Семантику слов пользователя
(«извлеки доходик» → SALES_REVENUE) сопоставляет МОДЕЛЬ, а не детерминированный
мост: у «доходик» и «Выручка» нет ни одной общей буквы, мосту такое не дано по
построению. Для выбора по смыслу модели нужен ПОЛНЫЙ список имён ячейки одним
небольшим файлом: полные статьи справочника (~100 КБ) в один `cat` не влезают.

В оглавлении ОБА вида показателей. Расчётные — чинят вторую давнюю дыру: в
справочнике .md их нет вовсе, и «чистый долг» модель там не находила, честно
решая «в реестре нет» и сочиняя свою формулу (жила её версия без вычета
депозитов — 141 138 вместо реестровых 80 961).

ПОЧЕМУ ПОБАЙТОВО ИЗ РЕЕСТРА. Движок читает compiled effective registry;
оглавление, собранное из чего-то другого или правленное руками, предложило бы
модели имена, по которым извлечение не пойдёт. Файлы генерируются
`catalogue.index_lines` и коммитятся; разошлись — пересобери:

    PYTHONPATH=src .venv/bin/python -c "
    from pathlib import Path
    from finengine.skills.metric_schema import PROFILE_IDS
    from finengine.skills.extraction.catalogue import index_lines
    к = Path('station-skills/aifa-extract/method/catalogue')
    for c in PROFILE_IDS:
        (к / f'_index_{c}.md').write_text('\\n'.join(index_lines(c)) + '\\n', encoding='utf-8')"

Подчёркивание в имени файла НЕ случайно: загрузчик справочника и его сторожа
перебирают `*.md`, отбрасывая `_`-префикс, — без него оглавление читалось бы
как одиннадцатая ячейка и ломало паритет составов.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from finengine.skills.extraction.catalogue import index_lines
from finengine.skills.metric_schema import PROFILE_IDS

КОРЕНЬ = Path(__file__).resolve().parents[1]
КАТАЛОГ = КОРЕНЬ / "station-skills" / "aifa-extract" / "method" / "catalogue"


@pytest.mark.parametrize("cell", PROFILE_IDS)
def test_the_index_file_matches_the_registry(cell: str):
    файл = КАТАЛОГ / f"_index_{cell}.md"
    assert файл.is_file(), (
        f"оглавления {файл.name} нет — модель выбирает показатели по смыслу "
        "именно из него; без файла она вернётся к слепому грепу")
    assert файл.read_text(encoding="utf-8") == "\n".join(index_lines(cell)) + "\n", (
        f"{файл.name} разошёлся с реестром движка — модель выберет имя, по "
        "которому извлечение не пойдёт. Пересборка — команда в шапке этого файла")


def test_the_index_fits_one_read():
    """Весь смысл файла — влезть в один `cat`; растолстеет — модель ослепнет."""
    for cell in PROFILE_IDS:
        размер = (КАТАЛОГ / f"_index_{cell}.md").stat().st_size
        assert размер < 40_000, (
            f"_index_{cell}.md вырос до {размер} байт — в один вывод команды "
            "уже не влезает, сократи синонимы (_INDEX_SYNONYMS)")


def test_the_calculated_are_listed_too():
    """Расчётные в оглавлении — ради них оно наполовину и заведено."""
    текст = (КАТАЛОГ / "_index_ifrs_nf_ru.md").read_text(encoding="utf-8")
    assert "## Расчётные" in текст
    assert "NET_DEBT — Чистый долг" in текст, (
        "расчётные пропали из оглавления: модель снова будет сочинять свой "
        "чистый долг вместо реестрового")
