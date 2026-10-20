"""Ручка уровня размышлений доходит до БОЕВОЙ модели (17.08.2026).

ЗАЧЕМ. Список рассуждающих моделей отставал от того, что реально работает.
В нём были только GLM и o1/o3, а шлюз работал на `qwen/qwen3.6-plus` — то есть
`reasoning` в запрос не попадал вовсе, и любая настройка уровня была
бездействующей. Замер 17.08 показал, что qwen размышляет по факту (4374 знака
размышлений на простой задаче), просто мы ему ничего про это не говорили.

Сторож привязан к НАСТРОЙКЕ, а не к списку литералов: модель в конфиге меняют
переменной окружения, и список обязан её покрывать — иначе ручка снова тихо
перестанет работать.
"""
from __future__ import annotations

import pytest

from finengine.config import settings
from finengine.llm.openrouter import _extra_body, _is_reasoning_model


def test_configured_model_is_recognised():
    """Главная проверка: та модель, что стоит в конфиге СЕЙЧАС, обязана
    получать параметр размышлений. Отрицательный контроль: сузил список до
    одного GLM — краснеет."""
    m = settings.openrouter_model
    assert _is_reasoning_model(m), (
        f"боевая модель «{m}» не в списке рассуждающих — параметр уровня "
        "размышлений до неё не доедет, и настройка станет бездействующей")


@pytest.mark.parametrize("model", [
    "qwen/qwen3.6-plus",
    "minimax/minimax-m3",
    "deepseek/deepseek-v4-flash",
    "z-ai/glm-5.2",
])
def test_candidates_are_covered(model):
    """Модели, которые мы замеряли 17.08 и держим в белом списке шлюза."""
    assert _is_reasoning_model(model)


@pytest.mark.parametrize("model", ["deepseek/deepseek-chat-v3-0324", "openrouter/auto"])
def test_non_reasoning_models_are_left_alone(model):
    """Не всё подряд: перевод заголовков приколот к deepseek-chat-v3 именно
    потому, что он НЕ размышляет и укладывается в тесный бюджет."""
    assert not _is_reasoning_model(model)


def test_effort_actually_lands_in_the_request():
    """Проверяем тело запроса, а не только предикат: между ними была вся суть
    дефекта."""
    body = _extra_body(settings.openrouter_model)
    assert body.get("reasoning", {}).get("effort") == "high", (
        f"в запрос уходит {body.get('reasoning')}, а не максимальный уровень")


def test_per_call_override_still_wins():
    """Поштучное переопределение должно оставаться сильнее умолчания —
    на нём держатся дешёвые служебные вызовы."""
    body = _extra_body(settings.openrouter_model, reasoning_effort="low")
    assert body["reasoning"]["effort"] == "low"


def test_openai_style_sends_the_level_as_a_top_level_parameter(monkeypatch):
    """OpenAI-совместимый шлюз (dev, 20.10.2026) молча игнорировал `reasoning: {effort}` в стиле
    OpenRouter: `low` и `high` не различались, и GLM-5.3 тратил весь вывод на размышления."""
    monkeypatch.setattr(settings, "reasoning_style", "openai")

    body = _extra_body(settings.openrouter_model, reasoning_effort="low")

    assert body == {"reasoning_effort": "low"}


def test_openrouter_style_stays_the_default():
    assert settings.reasoning_style == "openrouter"
