"""Production two-stage prompts must not change unnoticed (#147, 13.5.0).

ЗАЧЕМ. У анализа эталон промпта был, у извлечения — ни одного, и это дорого
обошлось: правка, замыкающая живой путь на файл ячейки, меняла текст инструкции
по всем восьми ячейкам, а предъявить разницу было нечем.

Первый же снимок показал то, чего никто не знал: ЯЧЕЙКИ ДЛЯ ПРОМПТА НЕ
СУЩЕСТВОВАЛИ. Все четыре нефинансовые давали ПОБАЙТОВО один и тот же промпт, все
четыре банковские — тоже. Два промпта на восемь ячеек, независимо от стандарта и
языка; `us_gaap_nf_en` получала инструкцию по-русски. Индийская и американская
отчётность обрабатывались русской инструкцией для МСФО.

Эталон снимается ПЕРЕХВАТОМ production boundary
(`benchmarks/snap_extract_prompt.py` подменяет `complete_with_usage`), а не
реконструкцией сборки. Harness идёт через тот же `extract_stream_from_text`,
что и station, и фиксирует обе стадии: compression и все extraction batches.
Модель не зовётся.

Падение здесь — приглашение посмотреть дифф, а не команда переснять эталон.
Переснимать осознанно:  .venv/bin/python benchmarks/snap_extract_prompt.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "tests" / "golden" / "extract"
sys.path.insert(0, str(REPO / "benchmarks"))

from snap_extract_prompt import CELLS, _capture  # noqa: E402


@pytest.mark.parametrize(("profile_id", "company_type"), CELLS, ids=[c for c, _ in CELLS])
def test_prompt_matches_golden(profile_id: str, company_type: str):
    prompts = asyncio.run(_capture(profile_id, company_type))
    assert prompts, f"{profile_id}: движок не дошёл до вызова модели"
    assert prompts[0][0] == "main-compression"
    assert any(stage.startswith("main-extract-") for stage, _ in prompts)
    for stage, got in prompts:
        name = f"{profile_id}.{stage}.txt"
        path = GOLDEN / name
        assert path.exists(), f"нет эталона {name} — снимите снимок и предъявите его в коммите"
        want = path.read_text(encoding="utf-8")
        assert got == want, (
            f"{name}: промпт разошёлся с эталоном. Если изменение НАМЕРЕННОЕ — "
            f"переснимите (benchmarks/snap_extract_prompt.py) и покажите дифф в коммите."
        )


def test_cells_produce_different_prompts():
    """Свойство, которого не было до 01.08: ячейка ВЛИЯЕТ на инструкцию.

    Проверяем не «промпт такой-то», а что восемь ячеек дают восемь разных
    текстов. Иначе возврат к общему реестру пройдёт мимо эталонов: они бы просто
    все стали одинаковыми, и каждый файл по отдельности «совпал» бы с собой.
    """
    seen: dict[str, list[str]] = {"bank": [], "non_financial": []}
    for profile_id, company_type in CELLS:
        path = GOLDEN / f"{profile_id}.main-compression.txt"
        if path.exists():
            seen[company_type].append(path.read_text(encoding="utf-8"))
    for company_type, texts in seen.items():
        assert len(set(texts)) == len(texts), (
            f"{company_type}: ячейки дают ОДИНАКОВЫЙ промпт — реестр ячейки снова "
            f"не доезжает до модели (ровно дефект #147)"
        )


def test_latin_aliases_present_in_russian_cell():
    """Отдельным сторожем: латиница в боевой русской банковской ячейке.

    Она терялась при переходе на файл ячейки (169 синонимов у всех 78
    показателей), а русские отчёты по МСФО часто двуязычные.
    """
    texts = sorted(GOLDEN.glob("ifrs_bank_ru.main-*.txt"))
    assert texts, "нет two-stage goldens для ifrs_bank_ru"
    text = "\n".join(path.read_text(encoding="utf-8") for path in texts)
    for alias in ("Total assets", "Qualified opinion"):
        assert alias in text, f"синоним «{alias}» пропал из эталонного промпта"


def test_golden_set_contains_no_legacy_full_context_names():
    names = [path.name for path in GOLDEN.glob("*.txt")]
    assert names
    assert not any("shard" in name for name in names)
