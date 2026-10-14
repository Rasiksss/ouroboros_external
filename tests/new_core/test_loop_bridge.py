"""The cross-loop bridge carries the caller's operation identifier to the far side.

These are pins on a guarantee the bridge gets from asyncio rather than from its own code.
`run_coroutine_threadsafe` schedules through `call_soon_threadsafe`, whose `Handle` captures
`contextvars.copy_context()` **in the calling thread** when no context is given, so the task on
the target loop inherits the caller's context — including `_CURRENT_TRACE_ID`. The bridge itself
does nothing to arrange this.

That is worth pinning precisely because it is invisible in the bridge's source. A rewrite that
passed an explicit `context=` to `call_soon_threadsafe`, or dispatched through a queue instead,
would silently strip the identifier from every MCP and outbound-A2A request, with no error and
no type change — `with_trace_header` omits the header rather than raising. These tests fail
loudly instead.

No pytest-asyncio in this tree: the lifespan loop is a real loop on its own thread, as in
production, and the caller drives it from the main thread or from `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest

from new_core.tools.loop_bridge import call_on_loop, call_on_loop_sync
from ouroboros.observability import aef_tracing

DASHED = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
#: The other spelling a caller may send. It is normalized to the dashed form, which is the one
#: spelling the agent stores, logs and puts on the wire.
PACKED = "f47ac10b58cc4372a5670e02b2c3d479"
OTHER = "463ac35c-9f64-13ad-4848-5a3953bb6124"


@pytest.fixture
def target_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A loop on its own thread, standing in for the app's lifespan loop."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


async def _observe() -> str:
    return aef_tracing.current_trace_id()


def test_the_bridged_call_observes_the_callers_identifier(target_loop):
    with aef_tracing.trace_scope(DASHED):
        observed = call_on_loop_sync(target_loop, _observe, timeout_sec=5)

    assert observed == DASHED


def test_the_identifier_is_visible_while_the_coroutine_is_built(target_loop):
    """A client that reads the context variable while assembling its request must see it.
    ``make_coro()`` runs in the caller's thread, inside the caller's scope, so it does."""
    at_build_time: list[str] = []

    def make_coro():
        at_build_time.append(aef_tracing.current_trace_id())
        return _observe()

    with aef_tracing.trace_scope(OTHER):
        call_on_loop_sync(target_loop, make_coro, timeout_sec=5)

    assert at_build_time == [OTHER]


def test_concurrent_bridged_calls_do_not_share_an_identifier(target_loop):
    # Both bodies wait on the target loop until the other has arrived, so the reads happen
    # with the two calls genuinely in flight together. The wait yields rather than blocking:
    # the target loop is single-threaded, and a blocking barrier there would deadlock it.
    arrived: list[str] = []
    both_in_flight = asyncio.Event()
    results: dict[str, str] = {}

    def call_under(trace_id: str) -> None:
        with aef_tracing.trace_scope(trace_id):

            async def observe_while_both_are_in_flight() -> str:
                arrived.append(trace_id)
                if len(arrived) == 2:
                    both_in_flight.set()
                await asyncio.wait_for(both_in_flight.wait(), timeout=5)
                return aef_tracing.current_trace_id()

            results[trace_id] = call_on_loop_sync(target_loop, observe_while_both_are_in_flight, timeout_sec=10)

    threads = [threading.Thread(target=call_under, args=(trace_id,)) for trace_id in (DASHED, OTHER)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(arrived) == 2
    assert results == {DASHED: DASHED, OTHER: OTHER}


def test_the_target_loop_keeps_nothing_after_the_call_completes(target_loop):
    with aef_tracing.trace_scope(PACKED):
        call_on_loop_sync(target_loop, _observe, timeout_sec=5)

    assert call_on_loop_sync(target_loop, _observe, timeout_sec=5) == ""


def test_a_call_made_outside_an_operation_carries_no_identifier(target_loop):
    """Carrying is not originating. A bridged call made outside any operation — the startup MCP
    tool-discovery refresh among them — must reach the far side with no identifier and send no
    header, rather than inventing one."""
    assert aef_tracing.current_trace_id() == ""

    observed = call_on_loop_sync(target_loop, _observe, timeout_sec=5)
    headers = call_on_loop_sync(target_loop, _observe_headers, timeout_sec=5)

    assert observed == ""
    assert aef_tracing.TRACE_HEADER_NAME not in headers


async def _observe_headers() -> dict[str, str]:
    return aef_tracing.with_trace_header({"Accept": "*/*"})


def test_the_asynchronous_entry_point_carries_the_identifier_too(target_loop):
    async def caller() -> str:
        with aef_tracing.trace_scope(DASHED):
            return await call_on_loop(target_loop, _observe, timeout_sec=5)

    assert asyncio.run(caller()) == DASHED
