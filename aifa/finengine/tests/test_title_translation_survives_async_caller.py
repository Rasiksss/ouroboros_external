"""Перевод заголовков работает, когда дайджест идёт из сценария (17.08.2026).

ЗАЧЕМ. `render_digest` синхронная, но единственный боевой вызывающий —
асинхронный конвейер (`pipeline.py:141`, `:272`), а внутри перевода стоял
`asyncio.run()`. Внутри работающего цикла он падает, отказ гасился общим
`except`, и в выпуске оставались английские заголовки ФРС, ЕЦБ и Минфина США.
Живой прогон 17.08 дал два таких отказа.

Сторож проверяет ровно боевое условие — вызов ИЗ РАБОТАЮЩЕГО ЦИКЛА. Проверка
той же функции из синхронного теста была бы зелёной и на сломанном коде:
`asyncio.run()` вне цикла отрабатывает нормально. Именно поэтому дефект и жил.
"""
from __future__ import annotations

import asyncio

from finengine import config as _config
from finengine.skills.news import render


def test_translation_runs_when_called_from_a_live_loop(monkeypatch, tmp_path):
    """Отрицательный контроль проверен: вернул `asyncio.run` на место —
    падает с «cannot be called from a running event loop»."""
    called: list[list[str]] = []

    async def _fake_batch(titles: list[str]) -> dict[str, str]:
        called.append(list(titles))
        return {t: f"РУ:{t}" for t in titles}

    monkeypatch.setattr(render, "_llm_translate_batch", _fake_batch)
    # Кэш переводов лежит в `settings.data_dir` и пишется на диск. Без подмены
    # первый тест оставил бы свой перевод в БОЕВОМ файле, а второй прочитал бы
    # его оттуда и не увидел бы отказа — так и случилось при первом прогоне.
    monkeypatch.setattr(_config.settings, "data_dir", str(tmp_path))

    async def _inside_running_loop() -> dict[str, str]:
        # Синхронный вызов внутри корутины — ровно то, что делает конвейер.
        return render._translate_titles_to_ru(["Fed holds rates steady", "Инфляция"])

    got = asyncio.run(_inside_running_loop())

    assert called, "перевод не был вызван — отказ снова гасится молча"
    assert got["Fed holds rates steady"] == "РУ:Fed holds rates steady", (
        "английский заголовок не переведён")
    assert got["Инфляция"] == "Инфляция", "русский заголовок не должен переводиться"


def test_failure_still_does_not_crash_the_digest(monkeypatch, tmp_path):
    """Правило «рендер не падает никогда» остаётся: сломанный перевод обязан
    вернуть исходные заголовки, а не уронить выпуск."""

    async def _boom(titles: list[str]) -> dict[str, str]:
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(render, "_llm_translate_batch", _boom)
    # Кэш переводов лежит в `settings.data_dir` и пишется на диск. Без подмены
    # первый тест оставил бы свой перевод в БОЕВОМ файле, а второй прочитал бы
    # его оттуда и не увидел бы отказа — так и случилось при первом прогоне.
    monkeypatch.setattr(_config.settings, "data_dir", str(tmp_path))

    async def _inside_running_loop() -> dict[str, str]:
        return render._translate_titles_to_ru(["Fed holds rates steady"])

    got = asyncio.run(_inside_running_loop())
    assert got["Fed holds rates steady"] == "Fed holds rates steady"
