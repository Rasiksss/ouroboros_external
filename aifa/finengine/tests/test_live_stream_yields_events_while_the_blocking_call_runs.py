"""Срез 31, П7 (backend): события синхронного конвейера доходят до потребителя
СРАЗУ, а не пачкой после завершения.

Прежний код копил события в списке (`event_sink=_gate_events.append`) и отдавал
их циклом `for ev in _gate_events: yield ev` уже после `await to_thread(...)`.
Поэтому план (`plan_graph`) и статусы узлов (`node_run`) появлялись в UI только
когда задача завершена. Тест держит блокирующую функцию «на паузе», пока
потребитель не получил первое событие: при буферизации потребитель никогда его
не получит, и тест упадёт по таймауту, а не пройдёт случайно.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from finengine.orchestrator.live_stream import stream_blocking


def test_the_first_event_is_delivered_before_the_blocking_call_returns():
    release = threading.Event()
    seen_before_finish: dict[str, bool] = {}

    def slow_pipeline(*, event_sink):
        event_sink({"type": "plan_graph"})
        # Ждём, пока потребитель подтвердит получение первого события. Если
        # события буферизуются до конца — подтверждения не будет никогда.
        seen_before_finish["released"] = release.wait(timeout=5)
        event_sink({"type": "node_run"})
        return "outcome"

    async def _go():
        got: list[tuple[str, object]] = []
        async for kind, payload in stream_blocking(slow_pipeline):
            got.append((kind, payload))
            if kind == "event" and payload["type"] == "plan_graph":
                release.set()
        return got

    got = asyncio.run(asyncio.wait_for(_go(), timeout=10))

    assert seen_before_finish["released"] is True, (
        "потребитель не получил plan_graph, пока функция ещё работала — события буферизуются до конца"
    )
    assert got == [
        ("event", {"type": "plan_graph"}),
        ("event", {"type": "node_run"}),
        ("result", "outcome"),
    ], "порядок событий сохраняется, результат приходит последним"


def test_an_exception_of_the_blocking_call_reaches_the_consumer_after_its_events():
    def failing_pipeline(*, event_sink):
        event_sink({"type": "plan_graph"})
        raise RuntimeError("boom")

    async def _go():
        got = []
        with pytest.raises(RuntimeError, match="boom"):
            async for kind, payload in stream_blocking(failing_pipeline):
                got.append((kind, payload))
        return got

    got = asyncio.run(asyncio.wait_for(_go(), timeout=10))
    assert got == [("event", {"type": "plan_graph"})], (
        "события, отправленные до исключения, не теряются; исключение не проглатывается"
    )


def test_positional_and_keyword_arguments_are_forwarded_and_the_sink_name_is_configurable():
    def pipeline(a, b, *, key, on_event):
        on_event({"a": a, "b": b, "key": key})
        return a + b

    async def _go():
        return [item async for item in stream_blocking(pipeline, 1, 2, key="k", sink_kw="on_event")]

    got = asyncio.run(asyncio.wait_for(_go(), timeout=10))
    assert got == [("event", {"a": 1, "b": 2, "key": "k"}), ("result", 3)]


def test_context_variables_of_the_caller_are_visible_inside_the_blocking_call():
    """`asyncio.to_thread` копирует contextvars — на них держится, например,
    тумблер «Поиск в интернете» (`openclaw.set_web_search`, task-local)."""
    import contextvars

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="unset")

    def pipeline(*, event_sink):  # noqa: ARG001
        return marker.get()

    async def _go():
        marker.set("from-caller")
        return [item async for item in stream_blocking(pipeline)]

    assert asyncio.run(asyncio.wait_for(_go(), timeout=10)) == [("result", "from-caller")]
