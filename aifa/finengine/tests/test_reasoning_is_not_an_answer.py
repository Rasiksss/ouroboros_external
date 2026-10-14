"""Размышления модели — не ответ.

ЖИВОЙ ИНЦИДЕНТ 11.08.2026, прогон ФосАгро. В openrouter.complete_with_usage
стояло:

    content = choice.content or ""
    if not content and choice.reasoning:
        content = choice.reasoning

Модель рассуждающая, токены размышлений считаются в max_completion_tokens.
На бюджете 6500 она израсходовала его целиком на размышления и до ответа не
дошла — content пустой. Код молча подставил внутренний монолог вместо отчёта,
и ВСЯ цепочка приняла его за анализ: разбор не нашёл ни одного раздела,
карточка вышла пустой, на экране осталась подпись под единственным графиком.
В сохранённом диалоге это видно дословно: «Here's a thinking process:» и обрыв
на полуслове «I need to make sure I».

Пустой ответ — это отказ. Подстановка превращала «не получилось» в «вот
результат».
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from finengine.llm import openrouter


class _FakeCompletions:
    def __init__(self, message, finish_reason="stop"):
        self._message = message
        self._finish = finish_reason
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=self._message, finish_reason=self._finish)],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=6500,
                                  prompt_tokens_details=None),
        )


def _install(monkeypatch, message, finish_reason="stop"):
    comp = _FakeCompletions(message, finish_reason)
    client = SimpleNamespace(chat=SimpleNamespace(completions=comp), closed=False)

    async def _close():
        client.closed = True

    client.close = _close
    monkeypatch.setattr(openrouter, "_client", lambda: client)
    return comp, client


def test_reasoning_never_becomes_the_answer(monkeypatch):
    """Ответа нет, размышления есть — это отказ, а не отчёт."""
    comp, client = _install(monkeypatch, SimpleNamespace(
        content="",
        reasoning="Here's a thinking process:\n1. Understand the request…",
    ))

    with pytest.raises(openrouter.ModelSilent) as err:
        asyncio.run(openrouter.complete_with_usage(prompt="дай анализ", model="glm-5.2"))

    assert "не выдала ответ" in str(err.value)
    # Повторять бессмысленно: бюджет тот же, исход тот же, а попытка стоит до 360 с.
    assert comp.calls == 1
    assert client.closed is True


def test_answer_is_returned_as_is(monkeypatch):
    """Ответ есть — размышления рядом ничего не меняют."""
    _, client = _install(monkeypatch, SimpleNamespace(
        content="## Резюме\nВыручка выросла.",
        reasoning="Here's a thinking process: …",
    ))

    text, usage = asyncio.run(
        openrouter.complete_with_usage(prompt="дай анализ", model="glm-5.2")
    )
    assert text.startswith("## Резюме")
    assert "thinking process" not in text
    assert usage["out"] == 6500
    assert client.closed is True


def test_empty_without_reasoning_is_also_a_refusal(monkeypatch):
    """Ни ответа, ни размышлений — тоже отказ, а не пустая строка наружу."""
    _, client = _install(monkeypatch, SimpleNamespace(content="", reasoning=None))

    with pytest.raises(openrouter.ModelSilent):
        asyncio.run(openrouter.complete_with_usage(prompt="дай анализ", model="glm-5.2"))
    assert client.closed is True


def test_stream_closes_client_after_the_last_chunk(monkeypatch):
    chunk = SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content="готово"))],
        usage=None,
    )

    async def _chunks():
        yield chunk

    class _StreamingCompletions:
        async def create(self, **_kwargs):
            return _chunks()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_StreamingCompletions()), closed=False,
    )

    async def _close():
        client.closed = True

    client.close = _close
    monkeypatch.setattr(openrouter, "_client", lambda: client)

    async def _collect():
        return [piece async for piece in openrouter.stream("дай анализ")]

    assert asyncio.run(_collect()) == ["готово"]
    assert client.closed is True
