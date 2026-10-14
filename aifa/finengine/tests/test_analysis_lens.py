"""Призмы: механизм есть, методика не тронута.

ЧТО СТОРОЖИМ (канон §3 и §8). Полный анализ ПЕРЕИСПОЛЬЗУЕТ куски кредитного,
риск- и факторного анализа, а не пишет свои. Механизм для этого — призмы:
файл `method/lens/<имя>.md` с элементами плюс две группы в `_order.md`
(«lens: <имя>» — правила, «framework: <имя>|<глубина>» — разделы).

ЧЕТЫРЕ ПРОВЕРКИ И ЗАЧЕМ КАЖДАЯ:
  1. Файлов призм НЕТ — промпт обязан быть тем же, что и до механизма. Иначе
     мы «добавили возможность», а на деле переписали каждый отчёт.
  2. Призма подключена — её правила стоят ПОСЛЕ правил отрасли, её разделы
     встроены в каркас ПЕРЕД выводами. Выводы, уехавшие в середину, — не
     мелочь: их там никто не ищет.
  3. Призма НЕ подключена, хотя файл лежит, — ни строчки в промпт. Иначе любая
     заведённая методологом призма молча стала бы обязательной для всех.
  4. Попросили несуществующую призму — ОТКАЗ. Молчание здесь дало бы отчёт,
     внешне неотличимый от кредитного, но без кредитного анализа.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from finengine.skills.analysis import methodology as md

_SRC = Path(__file__).resolve().parents[1] / "station-skills" / "aifa-analyze" / "method"

_LENS_FILE = """# Кредитная призма (фикстура теста)

## credit.rule.covenants
title: Ковенанты
kind: rule

Проверь ковенанты по кредитным договорам.

## credit.sec.debtor
title: Профиль заёмщика
kind: section

опиши профиль заёмщика.
"""


@pytest.fixture
def method_root(tmp_path, monkeypatch):
    """Копия боевой методики, куда тест вправе дописать призму."""
    root = tmp_path / "method"
    shutil.copytree(_SRC, root)
    monkeypatch.setenv("FE_METHOD_DIR", str(root))
    return root


def _add_lens(root: Path) -> None:
    (root / "lens").mkdir()
    (root / "lens" / "credit.md").write_text(_LENS_FILE, encoding="utf-8")
    order = root / "_order.md"
    order.write_text(
        order.read_text(encoding="utf-8")
        + "\n## lens: credit\n\n- credit.rule.covenants\n"
        + "\n## framework: credit|full\n\n- credit.sec.debtor\n",
        encoding="utf-8")


def test_without_lens_files_the_prompt_is_the_same(method_root):
    """Ноль призм — ноль изменений. Механизм добавлен, методика не тронута."""
    m = md.base()
    assert m.lens == {}, "призм нет, а словарь непустой — откуда-то приехала методика"
    frame = md.framework_block(m, "non_financial", "full")
    rules = md.analytical_block(m, "non_financial")
    assert frame == md.framework_block(m, "non_financial", "full", ())
    assert rules == md.analytical_block(m, "non_financial", None, ())


def test_a_lens_that_is_not_asked_for_stays_out(method_root):
    """Файл лежит, призму не просили — в промпте её нет."""
    _add_lens(method_root)
    m = md.base()
    assert "credit" in m.lens, "призма не прочиталась — глоб lens/*.md не сработал"
    assert "Ковенанты" not in md.analytical_block(m, "non_financial")
    assert "Профиль заёмщика" not in md.framework_block(m, "non_financial", "full")


def test_an_asked_lens_lands_in_both_places_and_before_conclusions(method_root):
    _add_lens(method_root)
    m = md.base()

    rules = md.analytical_block(m, "non_financial", None, ("credit",))
    # pin-ok: строка приходит ИЗ ДАННЫХ методики, а не из промпта-инструкции.
    # Предмет теста — работает ли фильтр линз, а не как сформулирован раздел.
    assert "Проверь ковенанты" in rules
    assert rules.index("Проверь ковенанты") > rules.index(
        m.text_of("nf.rule.profit_quality")[:40]), (
        "правило призмы встало ПЕРЕД правилами отрасли — порядок обратный")

    frame = md.framework_block(m, "non_financial", "full", ("credit",))
    # pin-ok: то же — раздел методики, отбираемый линзой.
    assert "Профиль заёмщика" in frame
    conclusions = m.element("common.sec.conclusions").title
    assert frame.index("Профиль заёмщика") < frame.index(conclusions), (
        "раздел призмы встал ПОСЛЕ выводов — вердикт уехал в середину отчёта")


def test_an_unknown_lens_is_a_refusal_not_a_silent_plain_report(method_root):
    _add_lens(method_root)
    m = md.base()
    with pytest.raises(md.MethodologyError) as e:
        md.framework_block(m, "non_financial", "full", ("нетакой",))
    assert "нетакой" in str(e.value) and "credit" in str(e.value), (
        "отказ не называет ни просимое, ни доступное — чинить его не по чему")
