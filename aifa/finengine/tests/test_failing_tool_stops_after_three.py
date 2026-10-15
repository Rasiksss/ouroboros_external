"""Отказавший инструмент перестают звать после трёх попыток (17.08.2026).

ЖИВОЙ ХОД ВЛАДЕЛЬЦА. Модель девять раз позвала `tavily_search`, каждый раз чуть
переформулировав запрос. Инструмент каждый раз возвращал ОДИН И ТОТ ЖЕ внятный
отказ, а ход умер по общему лимиту: вместо «поиск не работает» пользователь
увидел «agent ran more than 7 tool turns — aborted».

Готовая защита от повторов была, но она сверяет ТОЧНЫЕ аргументы — а менялись
именно они. Поэтому считаем отказы ПО ИМЕНИ инструмента, независимо от
аргументов. Порог три — решение владельца.

РЕВИЗИЯ 21.08.2026. Прежние пять сторожей искали строки в тексте `openclaw.py`.
Мутация `output_str[:400]` → `output_str[:0]` (счётчик отказов не растёт
никогда, то есть дефект возвращается целиком) оставляла зелёными ВСЕ ПЯТЬ.
Теперь петля хода запускается на поддельном потоке гейтвея, и считается не
текст, а число ДОШЕДШИХ до инструмента вызовов.
"""
from __future__ import annotations

import json

import pytest

from finengine.llm import openclaw

_TOOL = "tavily_search"
_ОТКАЗ = json.dumps({"error": "tavily: 401 unauthorized"}, ensure_ascii=False)
_УСПЕХ = json.dumps({"results": ["ЦБ повысил ключевую ставку"]}, ensure_ascii=False)


def _sse(*events: dict) -> list[str]:
    return [f"data: {json.dumps(e, ensure_ascii=False)}" for e in events] + ["data: [DONE]"]


def _turn_calling(query: str) -> list[str]:
    """Ход модели: один вызов инструмента. Запрос каждый раз НОВЫЙ — ровно так
    модель обходила защиту от повторов, которая сверяет точные аргументы."""
    return _sse(
        {"type": "response.output_item.done",
         "item": {"type": "function_call", "call_id": f"c-{query}", "name": _TOOL,
                  "arguments": json.dumps({"query": query})}},
        {"type": "response.completed", "response": {"usage": {}, "model": "fake"}},
    )


def _final_turn() -> list[str]:
    """Ход без вызовов — модель отвечает словами и петля заканчивается."""
    return _sse(
        {"type": "response.output_text.delta", "delta": "готово"},
        {"type": "response.completed", "response": {"usage": {}, "model": "fake"}},
    )


class _Stream:
    status_code = 200

    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def _fake_gateway(monkeypatch, turns: list[list[str]]):
    """Гейтвей, отдающий заранее написанный SSE — по списку строк на ход."""
    scripted = iter(turns)

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, **kw):
            return _Stream(next(scripted))

    monkeypatch.setattr(openclaw.httpx, "AsyncClient", _Client)
    # Токен гейтвея у петли обязателен — до поддельного потока она иначе
    # не доходит вовсе.
    monkeypatch.setattr(openclaw.settings, "openclaw_gateway_token", "тест", raising=False)


def _fake_tool(monkeypatch, answer: str) -> list[dict]:
    """Инструмент, всегда отвечающий одинаково. Возвращает журнал вызовов."""
    from finengine import observability
    from finengine.llm import tools as _tools

    calls: list[dict] = []

    async def _execute(name, args, ctx):
        calls.append({"name": name, "args": args})
        return answer

    monkeypatch.setattr(_tools, "execute", _execute)
    monkeypatch.setattr(observability, "emit", lambda **kw: None)
    return calls


async def _drain(**kw) -> list[dict]:
    return [ev async for ev in openclaw.stream_events("найди новости", **kw)]


@pytest.fixture
def turn(monkeypatch):
    """Ход из пяти обращений модели к одному инструменту + ответ словами."""
    def _run(answer: str):
        calls = _fake_tool(monkeypatch, answer)
        _fake_gateway(monkeypatch, [_turn_calling(q) for q in "абвгд"] + [_final_turn()])
        import asyncio
        return calls, asyncio.run(_drain(max_tool_iters=10))
    return _run


def test_a_failing_tool_is_not_called_a_fourth_time(turn):
    """Суть правки. Аргументы каждый раз новые — считать надо ПО ИМЕНИ:
    прежняя защита сверяла точные аргументы, и модель обходила её
    переформулировкой запроса."""
    calls, _events = turn(_ОТКАЗ)
    assert len(calls) == 3, (
        f"инструмент, отказавший трижды, позван {len(calls)} раз — модель снова "
        f"жжёт ход на переформулировках")
    assert len({json.dumps(c["args"], sort_keys=True) for c in calls}) == 3, (
        "аргументы совпали — проверка выродилась в защиту от повторов")


def test_the_blocked_call_tells_the_model_what_to_do(turn):
    """Молчаливая блокировка бесполезна: модель должна ПЕРЕСТАТЬ пробовать и
    сказать пользователю, что сломалось. Ровно этого не хватило в живом ходе."""
    _calls, events = turn(_ОТКАЗ)
    blocked = [e for e in events
               if e.get("type") == "tool_output"
               and "tool_disabled_this_turn" in e.get("output_full", "")]
    assert blocked, "заблокированный вызов не сказал модели ни слова"
    подсказка = blocked[0]["output_full"]
    for must in ("Не пробуй его снова", "ПРЯМО СКАЖИ"):
        assert must in подсказка, f"в подсказке модели нет «{must}»"


def test_a_working_tool_keeps_being_called(turn):
    """Успешный ответ не должен приближать блокировку — иначе рабочий
    инструмент отключится на четвёртом полезном вызове."""
    calls, _events = turn(_УСПЕХ)
    assert len(calls) == 5, (
        f"исправный инструмент позван {len(calls)} раз из 5 — счётчик отказов "
        f"считает удачные вызовы")


def test_the_counter_does_not_survive_the_turn(turn, monkeypatch):
    """Счётчик живёт внутри одного хода: инструмент, упавший вчера, сегодня
    обязан получить свои три попытки заново."""
    turn(_ОТКАЗ)
    calls, _events = turn(_ОТКАЗ)
    assert len(calls) == 3, (
        f"во втором ходе инструмент позван {len(calls)} раз — счётчик пережил ход")
