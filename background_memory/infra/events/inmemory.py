"""The in-process transport: one bounded queue, a publisher and a consumer over it.

Deliberately dumb — it delivers what it was given and nothing else. That is what
lets a Kafka provider replace it without a consumer changing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue as queue_mod
import threading

from background_memory.interfaces import (
    IMemoryEventConsumer,
    IMemoryEventPublisher,
    MemoryEvent,
)

log = logging.getLogger(__name__)

#: How many events the queue holds before publishing drops the newest. Every kind
#: shares it, and the drop is asymmetric: a lost consolidation trigger costs
#: nothing (the next agent reply re-triggers, and the handler re-reads the store),
#: while a lost distillation event takes the only copy of its response and trace
#: with it. Triggers are the more frequent kind — one per agent reply against one
#: per completed task — so the cap is sized for a burst of them to still leave
#: distillation room while one run occupies the loop.
MAX_PENDING_EVENTS = 32


class MemoryEventQueue:
    """The bounded hand-off: a synchronous put from any thread, an awaitable take.

    A `queue.Queue` underneath, because the putting half is called from whatever thread
    finished a user's work and must stay exactly what it was. What is added is the wake-up:
    a waiting consumer parks on an `asyncio.Event` of its own loop, and a put signals it
    through `call_soon_threadsafe`. Reading the queue on an executor instead would be a
    thread per poll, which is the arrangement this seam is being taken out of, and an
    abandoned poll would hold its thread until the read returned.
    """

    def __init__(self, maxsize: int) -> None:
        self._queue: queue_mod.Queue[MemoryEvent] = queue_mod.Queue(maxsize=int(maxsize))
        self._lock = threading.Lock()
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

    @property
    def maxsize(self) -> int:
        return self._queue.maxsize

    def put_nowait(self, event: MemoryEvent) -> None:
        """Raises ``queue.Full`` exactly as the bare queue did; never blocks the caller."""
        self._queue.put_nowait(event)
        with self._lock:
            waiters = list(self._waiters)
        for loop, ready in waiters:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(ready.set)

    def get_nowait(self) -> MemoryEvent:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()

    async def get(self, timeout: float) -> MemoryEvent | None:
        """One event, or ``None`` once ``timeout`` has passed, without holding the loop."""
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        waiter = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            self._waiters.append(waiter)
        try:
            while True:
                # Cleared before the attempt: a put landing between the two would otherwise
                # be cleared away and this caller would wait out its whole timeout.
                waiter[1].clear()
                try:
                    return self._queue.get_nowait()
                except queue_mod.Empty:
                    pass
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(waiter[1].wait(), remaining)
        finally:
            with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)


class InMemoryEventPublisher(IMemoryEventPublisher):
    """Puts events on a bounded queue, dropping the newest when it is full.

    Never blocks: the caller is the agent finishing a task, and a full memory
    queue must not hold up the user's reply.
    """

    def __init__(self, queue: MemoryEventQueue) -> None:
        self._queue = queue

    def publish(self, event: MemoryEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except queue_mod.Full:
            log.warning(
                "Memory event dropped: queue full (cap=%d) scope=%s",
                self._queue.maxsize,
                event.scope,
            )


class InMemoryEventConsumer(IMemoryEventConsumer):
    """Reads events off the same queue, waiting at most ``timeout`` seconds."""

    def __init__(self, queue: MemoryEventQueue) -> None:
        self._queue = queue

    async def poll(self, timeout: float = 0.0) -> MemoryEvent | None:
        if timeout <= 0:
            try:
                return self._queue.get_nowait()
            except queue_mod.Empty:
                return None
        return await self._queue.get(timeout)

    async def close(self) -> None:
        return None


__all__ = ["InMemoryEventConsumer", "InMemoryEventPublisher", "MemoryEventQueue"]
