"""Живая передача событий из синхронного конвейера в асинхронный SSE-поток.

Канонический конвейер (`GraphExecutor`) — синхронный код, который вызывается
через `asyncio.to_thread` (см. комментарий в `production_runtime.py`: модельные
атомы сами открывают свой event loop). Он сообщает о ходе работы вызовом
`event_sink(event)` из рабочего потока. Прежний `agent.query_stream` складывал
эти события в список и отдавал ПОСЛЕ завершения всей работы — поэтому план и
статусы узлов появлялись в UI только тогда, когда результат уже получен.

`stream_blocking` соединяет два мира очередью: рабочий поток кладёт событие в
`asyncio.Queue` через `loop.call_soon_threadsafe` (единственный потокобезопасный
способ), а корутина отдаёт его потребителю немедленно. Порядок FIFO сохраняется,
итоговое значение и исключение приходят ПОСЛЕ всех событий.
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

_EVENT = "event"
_RESULT = "result"
_ERROR = "error"


async def stream_blocking(
    fn: Callable[..., Any], /, *args: Any, sink_kw: str = "event_sink", cancel_kw: str | None = None,
    **kwargs: Any,
) -> AsyncIterator[tuple[str, Any]]:
    """Запустить блокирующий `fn` в потоке и отдавать его события по мере появления.

    `fn` получает именованный аргумент `sink_kw` (по умолчанию `event_sink`) —
    вызываемый объект `sink(event)`. Генератор выдаёт пары:

    * `("event", event)` — по одной на каждый вызов `sink`, сразу;
    * `("result", value)` — ровно один раз в конце, если `fn` вернулась;
    * исключение `fn` пробрасывается ПОСЛЕ уже отданных событий.

    Если задан `cancel_kw`, `fn` получает под этим именем вызываемый объект `is_cancelled()`: он становится
    истинным, когда потребитель ушёл (ход остановлен). Поток при этом не прерывается — он сам решает, стоит ли
    начинать новую дорогую работу.

    Контекстные переменные вызывающего копируются в поток (`asyncio.to_thread`).
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    cancelled = threading.Event()
    if cancel_kw is not None:
        kwargs[cancel_kw] = cancelled.is_set

    def _sink(event: Any) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (_EVENT, event))

    def _runner() -> None:
        try:
            value = fn(*args, **{**kwargs, sink_kw: _sink})
        except BaseException as exc:  # noqa: BLE001 — пробрасываем потребителю, не глотаем
            loop.call_soon_threadsafe(queue.put_nowait, (_ERROR, exc))
        else:
            loop.call_soon_threadsafe(queue.put_nowait, (_RESULT, value))

    worker = asyncio.create_task(asyncio.to_thread(_runner))
    finished = False
    try:
        while True:
            kind, payload = await queue.get()
            if kind == _ERROR:
                finished = True
                await worker  # `_runner` уже закончил — это лишь снятие задачи
                raise payload
            yield kind, payload
            if kind == _RESULT:
                finished = True
                await worker
                return
    finally:
        # Ранний выход потребителя (отмена хода) намеренно не ждёт конвейер: поток
        # дорабатывает сам, а его последние события просто никто не прочитает.
        if not finished:
            cancelled.set()


__all__ = ["stream_blocking"]
