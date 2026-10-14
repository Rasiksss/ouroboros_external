"""Бенчмарк обязан мерить ТОТ ЖЕ путь, которым идёт пользователь (#147).

ЗАЧЕМ. С 01.08 живой путь собирает промпт из реестра ЯЧЕЙКИ (`registry_override`),
а бенчмарк ходил в движок мимо него — брал из ячейки только имена показателей, а
синонимы, подсказки и формулы движок подставлял свои, из сгенерированного
`data/metrics.json`.

Значит бенчмарк мерил промпт, которого в продукте больше нет. Замер «после
переезда всё хорошо» не доказывал бы ничего про прод — а именно на такие замеры
опирается решение вливать или откатывать. Прибор обязан быть подключён к тому же
контуру, что и продукт.

Проверка статическая: прогон бенчмарка зовёт модель на реальных отчётах (минуты и
деньги), поэтому здесь сторожим ПРОВОДКУ, а совпадение самих промптов уже
проверено эталонами (`tests/test_extract_prompt_golden.py`).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "benchmarks"))


def test_benchmark_passes_the_cell_registry_to_the_engine():
    import inspect  # noqa: PLC0415

    import run_extract  # noqa: PLC0415

    src = inspect.getsource(run_extract._run_engine)
    assert "registry_override=" in src, (
        "бенчмарк снова ходит в движок мимо реестра ячейки — он мерит не тот промпт, "
        "который видит пользователь"
    )


def test_benchmark_uses_the_same_loader_as_production():
    """Не свой разбор json, а `registry_for_profile` — иначе прибор и продукт
    разойдутся при первой же правке загрузчика."""
    import inspect  # noqa: PLC0415

    import run_extract  # noqa: PLC0415

    src = inspect.getsource(run_extract._cell_registry)
    assert "registry_for_profile" in src, "бенчмарк читает реестр своим способом"


def test_cell_name_is_the_profile_id():
    """Проводка держится на том, что имя ячейки в бенчмарке = profile_id.

    Если это перестанет быть верным, реестр молча уедет не тот, и бенчмарк
    начнёт мерить чужую ячейку — без единой ошибки.
    """
    from bench_cells import CELLS  # noqa: PLC0415

    from finengine.skills.metric_schema import registry_for_profile  # noqa: PLC0415

    for cell in CELLS:
        reg = registry_for_profile(cell)
        assert reg.get("extractable"), f"{cell}: registry_for_profile не нашёл ячейку"
