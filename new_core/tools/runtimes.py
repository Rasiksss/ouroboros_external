"""The two local placements a tool body can run in.

A runtime answers one question — *where* does this body run — and nothing else. Gates run
before it in ``dispatch.execute_tool``, error text and ``result_meta`` are assembled after it
in ``_execute_single_tool``, truncation and the timeout belong to the caller above. A runtime
that repeated any of them would mean two places deciding, and one answer per runtime.

The distinction between the two is not "fast versus slow" but **whether the body blocks the
loop**:

- :class:`InlineToolRuntime` — the body is already asynchronous, or trivially fast. It runs
  on the calling coroutine's loop and blocks it for the duration.
- :class:`ThreadToolRuntime` — the body is blocking synchronous code. It runs on a worker
  thread, so the awaiting coroutine is free while it runs, and a gathered batch of such calls
  is real parallelism.

:data:`THREAD_RUNTIME` is the provider default, because ``IToolProvider.execute`` is a plain
synchronous function everywhere: an inline placement would make ``asyncio.gather`` over a
read-only batch run strictly one call after another, silently, with no test failing.

Both classes are stateless, which is why a single shared instance of each serves every
provider.

**Why ``ThreadToolRuntime`` owns a per-call executor instead of calling
``asyncio.to_thread``.** This is the paragraph to read before simplifying it back; both
reasons were measured, not argued.

1. ``to_thread`` submits to the loop's *default* executor, and on Python 3.10 the ``finally``
   block of ``asyncio.run`` ends with ``run_until_complete(loop.shutdown_default_executor())``
   — an unbounded join of every thread that executor holds. (The ``timeout`` parameter that
   would bound it is 3.12+.) A tool body that ignores its deadline would therefore stop being
   abandoned and start holding the whole round open: with a 0.5 s deadline over a 3 s body,
   ``to_thread`` returned the timeout at 0.50 s but ``asyncio.run`` only returned at 3.01 s,
   against 0.50 s for an owned executor.
2. A single process-wide pool has the same problem in a slower form: an abandoned thread
   occupies a slot for as long as the hung body runs, so N hangs starve every later tool call
   process-wide. One executor per call means a hang leaks one thread from its own already-dead
   pool and cannot reach the next call.

The per-call ``ThreadPoolExecutor(1)`` is not new cost — it is what ``_execute_with_timeout``
used to build for every call, moved down to where placement is decided.

``run_in_executor`` does not propagate ``contextvars`` (``to_thread`` does), so the copy is
explicit here. Crossing into another thread *is* the placement, so this is where that question
belongs; every future placement answers it for itself rather than relying on a helper in the
loop layer.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from new_core.tools.interfaces import IToolRuntime

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
    ) -> str:
        # A provider may return an awaitable instead of a result (see ``IToolProvider``):
        # this is how an asynchronous source stops bridging through a thread of its own.
        result: Any = provider.execute(ctx, name, args)
        if inspect.isawaitable(result):
            return await result
        return result


class ThreadToolRuntime(IToolRuntime):
    """Runs the body on a worker thread of its own, leaving the calling loop free."""

    async def run(
        self,
        provider: IToolProvider,
        ctx: ToolContext,
        name: str,
        args: dict[str, Any],
    ) -> str:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{name}")
        call = contextvars.copy_context()
        # ``submit`` + ``wrap_future`` is exactly what ``run_in_executor`` does for a supplied
        # executor; it is spelled out here only to keep a handle on the worker's future, so an
        # abandoned body's late outcome stays observable instead of being sealed in the wrapper.
        future: Future[Any] = executor.submit(call.run, provider.execute, ctx, name, args)
        # The worker settles the future while this coroutine unwinds, so both flags are read
        # and written from two threads. ``state`` makes "is this outcome ours to report" one
        # decision: the two reporters below race to claim it, never to both take it or both
        # leave it.
        state = threading.Lock()
        abandoned = False
        reported = False

        def _report_late_outcome(f: Future[Any]) -> None:
            # Reports an outcome nobody is waiting for. As a done callback it fires on the
            # worker thread when the body settles; the abandonment path below calls it directly
            # for a body that settled while this coroutine was already unwinding, whose
            # callback therefore ran too early to know. Once the round has stopped waiting
            # (``abandoned``), nothing retrieves this future and the concurrent future emits no
            # diagnostic of its own, so its outcome would vanish. A provider turns a body's own
            # exception into a ``⚠️ TOOL_ERROR`` *result*, so a late failure settles this future
            # successfully; the outcome worth an attributed line is therefore the result as much
            # as an exception — a mutative body that only finished (or errored) after we gave up
            # on it. A body whose outcome reached the awaiter is reported there, so it never
            # gets here.
            #
            # The two outcomes are not logged alike, because they are not alike. An exception is
            # the body's own failure and goes out whole — it is the only account of why the call
            # died, and nothing else records it. A result is arbitrary tool output — file
            # contents, credentials, whatever the body touched — and this line lands in the
            # central log, so it is never carried here: naming the abandoned call is what makes
            # the line actionable, and the payload never was.
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
            # Unwinding without the body's outcome is the abandonment — a cancelled deadline,
            # nearly always. The body's *own* failure unwinds through here too, as the very
            # object the future holds; that one was delivered, and the caller reports it.
            # Anything else leaves a body whose outcome nothing will collect.
            if future.done() and not future.cancelled() and future.exception() is unwind:
                raise
            with state:
                abandoned = True
            # Flagging this in a ``finally``, and only after finding the body unfinished, left
            # the window this closes: a body settling between the cancellation and the flag ran
            # its callback while the flag was still clear, and no second callback was coming —
            # so the late outcome went unreported. A future that has already settled is
            # therefore reported from here instead; ``reported`` keeps the two from doubling up.
            if future.done():
                _report_late_outcome(future)
            raise
        finally:
            # ``wait=False`` is the abandonment: a body that outlived its deadline finishes
            # on its own thread and nothing joins it. See the module docstring.
            executor.shutdown(wait=False)
        # Awaited here rather than on the worker thread — the awaitable belongs to whatever
        # loop is running, and a provider that returns one (D8) must not have to know where
        # its body was placed.
        if inspect.isawaitable(result):
            return await result
        return result


#: The shared inline runtime. Stateless, so one instance serves every descriptor that names
#: it: an already-asynchronous body, or one too trivial to be worth a thread.
INLINE_RUNTIME = InlineToolRuntime()

#: The shared thread runtime, and ``IToolProvider.default_runtime``. Stateless — the executor
#: it uses is created per call, not held here.
THREAD_RUNTIME = ThreadToolRuntime()
