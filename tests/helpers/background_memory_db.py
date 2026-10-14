"""One runtime database behind two engines, and the loop the asynchronous one is driven on.

Existing tests seed chat history, tasks and memory rows through the runtime repositories; the
background-memory repositories read the same database through the asynchronous engine. The
pairing lives here so the choice of engines is made once and the seeding call sites stay
word for word what they are.

The loop runs in a thread of its own because one asynchronous engine may be driven from
exactly one event loop: the handlers under test are coroutines, and a case drives them with
``db.run`` so their repository calls land on the loop their engine belongs to.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from sqlalchemy.ext.asyncio import AsyncEngine

from background_memory.infra.foreground import TaskTableForegroundPressure
from background_memory.infra.repositories import (
    SqlAuditRepo,
    SqlChatHistoryRepo,
    SqlConsolidationLeaseRepo,
    SqlContextSummaryRepo,
    SqlMemoryHeaderRepo,
    SqlTaskRepo,
)
from infra.async_engine import create_async_runtime_engine
from ouroboros.storage.runtime_persistence import runtime_persistence

_LOOP_TIMEOUT_SEC = 30.0


def llm_double(chat: Any) -> SimpleNamespace:
    """One synchronous answer behind the awaitable face the handlers call."""

    async def async_chat(**kwargs: Any) -> Any:
        return chat(**kwargs)

    return SimpleNamespace(chat=chat, async_chat=async_chat)


def mock_llm() -> MagicMock:
    """A ``MagicMock`` answering through ``chat``, so call assertions stay on that attribute."""
    llm = MagicMock()

    async def async_chat(**kwargs: Any) -> Any:
        return llm.chat(**kwargs)

    llm.async_chat = async_chat
    return llm


@dataclass(frozen=True)
class MemoryDatabase:
    """The synchronous store that seeds, and the repositories that read it."""

    store: Any
    engine: AsyncEngine
    loop: asyncio.AbstractEventLoop
    leases: SqlConsolidationLeaseRepo
    summaries: SqlContextSummaryRepo
    history: SqlChatHistoryRepo
    memory_headers: SqlMemoryHeaderRepo
    tasks: SqlTaskRepo
    audit: SqlAuditRepo

    def run(self, coro: Any) -> Any:
        """Drive one repository coroutine on the loop its engine belongs to."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=_LOOP_TIMEOUT_SEC)

    def foreground_pressure(self) -> TaskTableForegroundPressure:
        return TaskTableForegroundPressure(self.tasks)


def _loop_in_a_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    running = threading.Event()

    def _serve() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(running.set)
        loop.run_forever()

    thread = threading.Thread(target=_serve, name="test-memory-database-loop", daemon=True)
    thread.start()
    running.wait(timeout=_LOOP_TIMEOUT_SEC)
    return loop, thread


def memory_database() -> Iterator[MemoryDatabase]:
    """Pair the test's composed store with repositories over the same database."""
    store = runtime_persistence.current
    loop, thread = _loop_in_a_thread()
    try:
        engine = create_async_runtime_engine(store.engine.url.render_as_string(hide_password=False))
        try:
            yield MemoryDatabase(
                store=store,
                engine=engine,
                loop=loop,
                leases=SqlConsolidationLeaseRepo(engine),
                summaries=SqlContextSummaryRepo(engine),
                history=SqlChatHistoryRepo(engine),
                memory_headers=SqlMemoryHeaderRepo(engine),
                tasks=SqlTaskRepo(engine),
                audit=SqlAuditRepo(engine),
            )
        finally:
            asyncio.run_coroutine_threadsafe(engine.dispose(), loop).result(timeout=_LOOP_TIMEOUT_SEC)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=_LOOP_TIMEOUT_SEC)
        loop.close()


__all__ = ["MemoryDatabase", "llm_double", "memory_database", "mock_llm"]
