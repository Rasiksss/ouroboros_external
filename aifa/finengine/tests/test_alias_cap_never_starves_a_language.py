"""Потолок синонимов делится между языками, а не отдаётся первому (17.08.2026).

КАК НАШЛОСЬ. Дописал в `DEPRECIATION_AMORTIZATION` четыре русские формулировки
из отчёта Мегафона — и из промпта пропали ВСЕ английские (`D&A`, `DD&A`,
`Depreciation`), хотя их никто не трогал. Оба сборщика промпта резали
СКЛЕЕННЫЙ список: `(aliases_ru + aliases_en)[:N]`. Пока русских было двое,
английские помещались; шестой русский синоним вытеснил их целиком.

ЧЕМ ЭТО ОПАСНО СВЕРХ САМОЙ ПОТЕРИ. Отказ немой в трёх местах сразу: реестр
показывает оба списка, тест на синонимы зелёный, паспорт прогона называет
полный состав — а до модели доезжает половина. Заметить можно было только
глазами в эталоне промпта, и то если знать, куда смотреть.

Русские отчёты по МСФО двуязычны, и английский заголовок в них встречается не
реже русского (замер 01.08: 192 латинских синонима на 75 показателей
`ifrs_nf_ru`), так что цена вытеснения — прямые пропуски в таблице.
"""
from __future__ import annotations

import pytest

from finengine.skills.bulk_extractor import _merge_aliases_from_base
from finengine.skills.metric_schema import merge_aliases, registry_for_profile
from finengine.skills.two_stage_extractor import _metric_info_blocks


def test_the_regression_itself_russian_growth_does_not_evict_english():
    """Тот самый случай: шесть русских, три английских, потолок восемь."""
    metric = {
        "aliases_ru": [f"ру-{i}" for i in range(6)],
        "aliases_en": ["D&A", "DD&A", "Depreciation"],
    }
    got = merge_aliases(metric, 8)
    assert len(got) == 8
    assert any(a.startswith("ру-") for a in got), "русские вытеснены"
    assert any(a in ("D&A", "DD&A", "Depreciation") for a in got), (
        f"английские вытеснены целиком — ровно дефект 17.08: {got}")


@pytest.mark.parametrize("ru,en", [(1, 30), (30, 1), (4, 4), (0, 9), (9, 0)])
def test_cap_is_respected_and_the_minority_language_keeps_a_seat(ru, en):
    """Перекос в любую сторону: потолок соблюдён, непустой язык не исчезает."""
    metric = {
        "aliases_ru": [f"ру-{i}" for i in range(ru)],
        "aliases_en": [f"en-{i}" for i in range(en)],
    }
    got = merge_aliases(metric, 6)
    assert len(got) <= 6
    if ru and en:
        assert any(a.startswith("ру-") for a in got), f"русский язык выпал: {got}"
        assert any(a.startswith("en-") for a in got), f"английский выпал: {got}"


def test_empty_and_placeholder_aliases_do_not_take_seats():
    """«Нет» и пустые строки — заполнитель реестра, а не синоним: занимать
    место в промпте они не имеют права."""
    metric = {"aliases_ru": ["Нет", "", "Выручка"], "aliases_en": ["Revenue"]}
    assert merge_aliases(metric, 8) == ["Выручка", "Revenue"]


def test_the_shipped_prompt_keeps_both_languages_for_amortisation():
    """Сквозная проверка на СБОРКЕ промпта боевой схемы, а не на функции.

    До 21.08 здесь читался эталон `tests/golden/extract/ifrs_nf_ru.shard2.txt`.
    Это охраняло СНИМОК, а не код: мутация `merge_aliases` обратно в
    `(ru + en)[:limit]` роняет тесты выше и оставляет эту проверку зелёной —
    файл эталона от правки движка не меняется. Вдобавок эталон снят со схемы
    `full_context`, а боевая с 16.08 — двухстадийная (см. докстроку
    `benchmarks/snap_extract_prompt.py:_capture`).

    Поэтому цепочка гоняется живьём и ровно та, что уходит в модель:
    ячейка → слияние синонимов с общим реестром → блок показателя в промпте.
    Проверено мутацией 21.08: `_merge_aliases_from_base` → `return rows` роняет
    этот тест (эталонный вариант остался бы зелёным — .txt от правки кода не
    меняется). ⚠️ САМ ПОТОЛОК он сегодня не сторожит: у боевой схемы он равен
    восьми, а в `ifrs_nf_ru` нет показателя с восемью русскими синонимами при
    непустой латинице — вытеснения не наступает. Потолок держат три теста выше.
    """
    rows = _merge_aliases_from_base(
        registry_for_profile("ifrs_nf_ru")["extractable"], "non_financial")
    metric = next(m for m in rows if m["enum"] == "DEPRECIATION_AMORTIZATION")
    block = _metric_info_blocks([metric])[0]
    line = next(ln for ln in block.splitlines() if "называться" in ln)
    assert "АФПП" in line, "разнесённая русская формулировка не доехала до промпта"
    assert "D&A" in line, "английские синонимы вытеснены русскими (дефект 17.08)"
