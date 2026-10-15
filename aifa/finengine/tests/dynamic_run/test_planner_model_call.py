"""Вызов модели планировщика цикла: глубина размышлений — настройка, длительность успешного ответа — в логе.

Разбор прогонов на dev 09.10.2026: планировщик отвечал от 17 до 345 с (`reasoning_effort="high"`,
`max_output=16000`), а в логе была только строка о таймауте, поэтому медленные, но успешные
вызовы не разобрать."""
from __future__ import annotations

import asyncio
import logging

from finengine.config import Settings
from finengine.llm import openrouter
from finengine.skills.spec import dynamic_run


def _capture(monkeypatch) -> dict:
    seen: dict = {}

    async def _complete(prompt, **kwargs):
        seen.update(kwargs)
        return "{}"

    monkeypatch.setattr(openrouter, "complete", _complete)
    return seen


def test_the_planner_reasoning_effort_is_a_setting_with_the_old_default(monkeypatch) -> None:
    assert Settings.model_fields["planner_reasoning_effort"].default == "high"
    seen = _capture(monkeypatch)

    asyncio.run(dynamic_run._default_complete("system", "prompt"))

    assert seen["reasoning_effort"] == "high"


def test_the_planner_call_follows_the_configured_reasoning_effort(monkeypatch) -> None:
    seen = _capture(monkeypatch)
    monkeypatch.setattr(dynamic_run.settings, "planner_reasoning_effort", "medium")

    asyncio.run(dynamic_run._default_complete("system", "prompt"))
    asyncio.run(dynamic_run.complete_plan("system", "prompt"))

    assert seen["reasoning_effort"] == "medium"


def test_a_successful_planner_call_leaves_its_duration_and_sizes_in_the_log(monkeypatch, caplog) -> None:
    _capture(monkeypatch)

    with caplog.at_level(logging.INFO, logger=dynamic_run.log.name):
        asyncio.run(dynamic_run._default_complete("системный", "промпт хода"))

    line = next((record.getMessage() for record in caplog.records if "планировщик цикла" in record.getMessage()), "")
    assert "ответил за" in line and "промпт 11 знаков" in line and "системный 9" in line and "ответ 2 знаков" in line
