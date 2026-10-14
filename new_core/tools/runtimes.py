"""Local tool placement; threaded bodies remain owned through process drain."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING, Any

from new_core.tools.interfaces import IToolRuntime, ToolResult

if TYPE_CHECKING:
    from new_core.tools.context import ToolContext
    from new_core.tools.interfaces import IToolProvider

__all__ = ["INLINE_RUNTIME", "THREAD_RUNTIME", "InlineToolRuntime", "ThreadToolRuntime"]

log = logging.getLogger(__name__)


class InlineToolRuntime(IToolRuntime):
    """Runs the body on the calling coroutine's loop. No thread, no loop of its own."""

    async def run(
        self,
        provider: IToolProvider,
        ctx: ToolContext,
        name: str,
        args: dict[str, Any],
    ) -> str | ToolResult:
        result: Any = provider.execute(ctx, name, args)
        if inspect.isawaitable(result):
            return await result
        return result


class ThreadToolRuntime(IToolRuntime):
    """Runs the body on a worker thread of its own, leaving the calling loop free."""

    def __init__(self) -> None:
        self._bodies: set[Future[Any]] = set()
        self._lock = threading.Lock()

    def drain(self, *, deadline_sec: float) -> bool:
        """Wait for worker bodies after the caller has stopped admitting agent work."""
        deadline = time.monotonic() + max(0.0, deadline_sec)
        while True:
            with self._lock:
                bodies = tuple(self._bodies)
            if not bodies:
                return True
            if wait(bodies, timeout=max(0.0, deadline - time.monotonic())).not_done:
                return False

    def _finished(self, future: Future[Any]) -> None:
        with self._lock:
            self._bodies.discard(future)

    async def run(
        self,
        provider: IToolProvider,
        ctx: ToolContext,
        name: str,
        args: dict[str, Any],
    ) -> str | ToolResult:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{name}")
        call = contextvars.copy_context()
        with self._lock:
            future: Future[Any] = executor.submit(call.run, provider.execute, ctx, name, args)
            self._bodies.add(future)
        future.add_done_callback(self._finished)
        state = threading.Lock()
        abandoned = False
        reported = False

        def _report_late_outcome(f: Future[Any]) -> None:
            nonlocal reported
            with state:
                if not abandoned or reported or f.cancelled():
                    return
                reported = True
            exc = f.exception()
            if exc is not None:
                log.warning(
                    "Abandoned tool body raised after its deadline (task_id=%s tool=%s): %r",
                    ctx.task_id,
                    name,
                    exc,
                )
            else:
                log.warning(
                    "Abandoned tool body finished after its deadline, result discarded (task_id=%s tool=%s)",
                    ctx.task_id,
                    name,
                )

        future.add_done_callback(_report_late_outcome)
        try:
            result: Any = await asyncio.wrap_future(future)
        except BaseException as unwind:
            if future.done() and not future.cancelled() and future.exception() is unwind:
                raise
            with state:
                abandoned = True
            if future.done():
                _report_late_outcome(future)
            raise
        finally:
            executor.shutdown(wait=False)
        if inspect.isawaitable(result):
            return await result
        return result


INLINE_RUNTIME = InlineToolRuntime()

THREAD_RUNTIME = ThreadToolRuntime()
