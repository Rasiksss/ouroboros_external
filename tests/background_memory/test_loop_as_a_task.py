"""The loop as a task: concurrent deliveries, the pair gate, the drain, and liveness."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from background_memory.interfaces import (
    ChatMessagePersistedEvent,
    ConsolidationContinuationEvent,
    FollowUp,
    IMemoryEventConsumer,
    MemoryEvent,
)
from background_memory.runtime import MAX_WAITING_PER_PAIR, MemoryRuntime
from new_core.identity import OwnerScope
from ouroboros.observability.aef_tracing import current_trace_id
from ouroboros.observability.health import LoopHeartbeat
from tests.helpers.shared import TEST_SCOPE, scope_with

DRAIN_SEC = 5.0
LEASE_WINDOW_SEC = 720.0

_OTHER = scope_with(TEST_SCOPE, owner_id="other-owner")
_TRACE_A = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
_TRACE_B = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


class _Feed(IMemoryEventConsumer):
    """Hands out the scripted events one per poll, then nothing. Always yields."""

    def __init__(self, events: list[MemoryEvent]) -> None:
        self._events = list(events)

    async def poll(self, timeout: float = 0.0) -> MemoryEvent | None:
        await asyncio.sleep(0)
        return self._events.pop(0) if self._events else None

    async def close(self) -> None:
        return None


def _trigger(scope: OwnerScope = TEST_SCOPE, message_id: str = "message-0001", trace_id: str = "") -> MemoryEvent:
    return ChatMessagePersistedEvent(scope=scope, message_id=message_id, trace_id=trace_id)


def _runtime(consumer: IMemoryEventConsumer, handlers: dict, stop_event: threading.Event, **kwargs) -> MemoryRuntime:
    return MemoryRuntime(
        consumer,
        handlers=handlers,
        stop_event=stop_event,
        heartbeat=kwargs.pop("heartbeat", LoopHeartbeat("test", stall_after_sec=LEASE_WINDOW_SEC)),
        drain_timeout_sec=kwargs.pop("drain_timeout_sec", DRAIN_SEC),
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


async def _until(predicate, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.005)
    return True


# --- concurrency --------------------------------------------------------------


def test_a_slow_delivery_for_one_pair_does_not_delay_another_pairs_event() -> None:
    """Inline dispatch made one owner's model call every other owner's wait."""
    stop_event = threading.Event()
    slow = asyncio.Event()
    delivered: list[str] = []

    async def handle(event: MemoryEvent) -> FollowUp | None:
        if event.scope == TEST_SCOPE:
            await slow.wait()
        delivered.append(event.scope.owner_id)
        return None

    runtime = _runtime(_Feed([_trigger(), _trigger(_OTHER)]), {ChatMessagePersistedEvent: handle}, stop_event)

    async def _scenario() -> None:
        running = asyncio.ensure_future(runtime.execute())
        assert await _until(lambda: delivered == [_OTHER.owner_id]), "the second pair waited on the first"
        slow.set()
        assert await _until(lambda: len(delivered) == 2)
        stop_event.set()
        await asyncio.wait_for(running, timeout=5)

    _run(_scenario())

    assert delivered == [_OTHER.owner_id, TEST_SCOPE.owner_id]


def test_two_concurrent_deliveries_each_log_under_their_own_operation() -> None:
    """One scope around the loop would give both overlapping deliveries the same identifier."""
    stop_event = threading.Event()
    both_in = asyncio.Event()
    seen: dict[str, str] = {}
    arrived: list[str] = []

    async def handle(event: MemoryEvent) -> FollowUp | None:
        arrived.append(event.scope.owner_id)
        if len(arrived) == 2:
            both_in.set()
        await both_in.wait()
        seen[event.scope.owner_id] = current_trace_id()
        return None

    runtime = _runtime(
        _Feed([_trigger(trace_id=_TRACE_A), _trigger(_OTHER, trace_id=_TRACE_B)]),
        {ChatMessagePersistedEvent: handle},
        stop_event,
    )

    async def _scenario() -> None:
        running = asyncio.ensure_future(runtime.execute())
        assert await _until(lambda: len(seen) == 2)
        stop_event.set()
        await asyncio.wait_for(running, timeout=5)

    _run(_scenario())

    assert seen == {TEST_SCOPE.owner_id: _TRACE_A, _OTHER.owner_id: _TRACE_B}


# --- the pair gate ------------------------------------------------------------


def test_a_burst_for_one_pair_produces_deliveries_that_never_overlap() -> None:
    """The gate is correctness: two deliveries for one pair share the handler's run object."""
    stop_event = threading.Event()
    live = 0
    peak = 0
    handled = 0

    async def handle(event: MemoryEvent) -> FollowUp | None:
        nonlocal live, peak, handled
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        handled += 1
        return None

    burst = [_trigger(message_id=f"message-{index:04d}") for index in range(6)]
    runtime = _runtime(_Feed(burst), {ChatMessagePersistedEvent: handle}, stop_event)

    async def _scenario() -> None:
        running = asyncio.ensure_future(runtime.execute())
        assert await _until(lambda: handled == len(burst), timeout=10)
        stop_event.set()
        await asyncio.wait_for(running, timeout=5)

    _run(_scenario())

    assert (peak, handled) == (1, len(burst))


def test_one_pairs_run_object_is_written_by_one_delivery_at_a_time() -> None:
    """A case that would pass with no gate at all: two pairs running is not the question.

    The handler writes the shared object the consolidation handler keys by pair, in two steps
    with a suspension between them. Without the gate the two deliveries interleave and the
    object ends up holding one delivery's first step under the other's second.
    """
    stop_event = threading.Event()
    run_object: dict[str, str] = {}
    torn: list[tuple[str, str]] = []
    handled = 0

    async def handle(event: MemoryEvent) -> FollowUp | None:
        nonlocal handled
        mine = event.message_id
        run_object["started"] = mine
        await asyncio.sleep(0)
        run_object["finished"] = mine
        if run_object["started"] != mine:
            torn.append((run_object["started"], mine))
        handled += 1
        return None

    burst = [_trigger(message_id=f"message-{index:04d}") for index in range(5)]
    runtime = _runtime(_Feed(burst), {ChatMessagePersistedEvent: handle}, stop_event)

    async def _scenario() -> None:
        running = asyncio.ensure_future(runtime.execute())
        assert await _until(lambda: handled == len(burst), timeout=10)
        stop_event.set()
        await asyncio.wait_for(running, timeout=5)

    _run(_scenario())

    assert torn == []


def test_a_pairs_waiting_deliveries_are_bounded_rather_than_accumulated(caplog) -> None:
    stop_event = threading.Event()
    release = asyncio.Event()
    handled: list[str] = []

    async def handle(event: MemoryEvent) -> FollowUp | None:
        await release.wait()
        handled.append(event.message_id)
        return None

    over = MAX_WAITING_PER_PAIR + 4
    burst = [_trigger(message_id=f"message-{index:04d}") for index in range(over)]
    runtime = _runtime(_Feed(burst), {ChatMessagePersistedEvent: handle}, stop_event)

    async def _scenario() -> None:
        with caplog.at_level("WARNING"):
            running = asyncio.ensure_future(runtime.execute())
            assert await _until(lambda: any("already waiting" in r.getMessage() for r in caplog.records))
            release.set()
            stop_event.set()
            await asyncio.wait_for(running, timeout=5)

    _run(_scenario())

    assert len(handled) <= MAX_WAITING_PER_PAIR + 1


def test_a_due_follow_up_is_delivered_even_when_its_pair_is_at_capacity(caplog) -> None:
    """A dropped continuation strands its run and parks its pair for a whole lease window."""
    stop_event = threading.Event()
    hold = asyncio.Event()
    delivered: list[str] = []
    first = True

    async def handle(event: MemoryEvent) -> FollowUp | None:
        nonlocal first
        if first:
            first = False
            return FollowUp(ConsolidationContinuationEvent(scope=TEST_SCOPE), 0.05)
        await hold.wait()
        delivered.append(event.message_id)
        return None

    async def resume(event: MemoryEvent) -> FollowUp | None:
        delivered.append("continuation")
        return None

    burst = [_trigger(message_id=f"message-{index:04d}") for index in range(MAX_WAITING_PER_PAIR + 4)]
    runtime = _runtime(
        _Feed(burst),
        {ChatMessagePersistedEvent: handle, ConsolidationContinuationEvent: resume},
        stop_event,
    )

    async def _scenario() -> None:
        with caplog.at_level("WARNING"):
            running = asyncio.ensure_future(runtime.execute())
            assert await _until(lambda: any("already waiting" in r.getMessage() for r in caplog.records))
            assert await _until(lambda: bool(runtime._pending)), "the run asked for no continuation"
            assert await _until(lambda: not runtime._pending), "the continuation never came due"
            hold.set()
            assert await _until(lambda: "continuation" in delivered), "the pair's lane cap dropped it"
            stop_event.set()
            await asyncio.wait_for(running, timeout=5)

    _run(_scenario())

    assert delivered.index("continuation") < delivered.index("message-0002"), "it waited behind the queue"


def test_a_follow_up_queued_when_the_loop_stops_is_still_delivered() -> None:
    """The run that asked for it holds the pair's lease and only a delivery can release it."""
    stop_event = threading.Event()
    hold = asyncio.Event()
    delivered: list[str] = []
    first = True

    async def handle(event: MemoryEvent) -> FollowUp | None:
        nonlocal first
        if first:
            first = False
            return FollowUp(ConsolidationContinuationEvent(scope=TEST_SCOPE), 0.05)
        await hold.wait()
        delivered.append(event.message_id)
        return None

    async def resume(event: MemoryEvent) -> FollowUp | None:
        delivered.append("continuation")
        return None

    burst = [_trigger(message_id=f"message-{index:04d}") for index in range(3)]
    runtime = _runtime(
        _Feed(burst),
        {ChatMessagePersistedEvent: handle, ConsolidationContinuationEvent: resume},
        stop_event,
    )

    async def _scenario() -> None:
        running = asyncio.ensure_future(runtime.execute())
        assert await _until(lambda: bool(runtime._pending)), "the run asked for no continuation"
        assert await _until(lambda: not runtime._pending), "the continuation never came due"
        stop_event.set()
        hold.set()
        await asyncio.wait_for(running, timeout=5)

    _run(_scenario())

    assert "continuation" in delivered, "a stopping lane discarded the run's own continuation"


# --- stop and drain -----------------------------------------------------------


def test_stopping_waits_for_a_running_delivery_to_release_what_it_holds() -> None:
    stop_event = threading.Event()
    started = asyncio.Event()
    released: list[str] = []

    async def handle(event: MemoryEvent) -> FollowUp | None:
        started.set()
        try:
            await asyncio.sleep(0.2)
        finally:
            released.append(event.message_id)
        return None

    runtime = _runtime(_Feed([_trigger()]), {ChatMessagePersistedEvent: handle}, stop_event)

    async def _scenario() -> None:
        running = asyncio.ensure_future(runtime.execute())
        await asyncio.wait_for(started.wait(), timeout=5)
        stop_event.set()
        await asyncio.wait_for(running, timeout=5)

    _run(_scenario())

    assert released == ["message-0001"], "the loop returned before its delivery had finished"


def test_a_delivery_cancelled_at_the_drain_bound_still_releases_its_lease() -> None:
    """A release that is cancelled at its first suspension point releases nothing."""
    stop_event = threading.Event()
    started = asyncio.Event()
    released: list[str] = []

    async def _release() -> None:
        await asyncio.sleep(0.01)
        released.append("lease")

    async def handle(event: MemoryEvent) -> FollowUp | None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.shield(_release())
            raise
        return None

    runtime = _runtime(_Feed([_trigger()]), {ChatMessagePersistedEvent: handle}, stop_event, drain_timeout_sec=0.2)

    async def _scenario() -> None:
        running = asyncio.ensure_future(runtime.execute())
        await asyncio.wait_for(started.wait(), timeout=5)
        stop_event.set()
        await asyncio.wait_for(running, timeout=5)

    _run(_scenario())

    assert released == ["lease"]


def test_the_whole_drain_fits_the_one_bound_it_states() -> None:
    """Two full bounds is twice what the tier's stop_grace_period budgets."""
    stop_event = threading.Event()
    started = asyncio.Event()
    finish = asyncio.Event()

    async def handle(event: MemoryEvent) -> FollowUp | None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.shield(finish.wait())
            raise
        return None

    runtime = _runtime(_Feed([_trigger()]), {ChatMessagePersistedEvent: handle}, stop_event, drain_timeout_sec=0.4)

    async def _scenario() -> float:
        running = asyncio.ensure_future(runtime.execute())
        await asyncio.wait_for(started.wait(), timeout=5)
        stop_event.set()
        began = time.monotonic()
        await asyncio.wait_for(running, timeout=5)
        elapsed = time.monotonic() - began
        finish.set()
        await asyncio.sleep(0.05)
        return elapsed

    elapsed = _run(_scenario())

    assert elapsed < 0.65, f"the drain took {elapsed:.2f}s, more than the one bound it states"


def test_a_stopping_lane_starts_no_further_delivery() -> None:
    """Only consolidation reads the stop signal itself; a new delivery takes a fresh lease."""
    stop_event = threading.Event()
    started = asyncio.Event()
    release = asyncio.Event()
    handled: list[str] = []

    async def handle(event: MemoryEvent) -> FollowUp | None:
        started.set()
        await release.wait()
        handled.append(event.message_id)
        return None

    burst = [_trigger(message_id=f"message-{index:04d}") for index in range(3)]
    runtime = _runtime(_Feed(burst), {ChatMessagePersistedEvent: handle}, stop_event)

    async def _scenario() -> None:
        running = asyncio.ensure_future(runtime.execute())
        await asyncio.wait_for(started.wait(), timeout=5)
        assert await _until(lambda: len(runtime._lanes[(TEST_SCOPE.owner_id, TEST_SCOPE.agent_id)].waiting) == 2)
        stop_event.set()
        release.set()
        await asyncio.wait_for(running, timeout=5)

    _run(_scenario())

    assert handled == ["message-0000"], "the lane kept serving its queue while the loop was draining"


# --- liveness -----------------------------------------------------------------


def test_a_delivery_stuck_past_the_lease_window_reports_unhealthy_while_the_loop_polls() -> None:
    stop_event = threading.Event()
    started = asyncio.Event()
    heartbeat = LoopHeartbeat("test", stall_after_sec=0.05)

    async def handle(event: MemoryEvent) -> FollowUp | None:
        started.set()
        await asyncio.sleep(5)
        return None

    runtime = _runtime(_Feed([_trigger()]), {ChatMessagePersistedEvent: handle}, stop_event, heartbeat=heartbeat)

    async def _scenario() -> str:
        running = asyncio.ensure_future(runtime.execute())
        await asyncio.wait_for(started.wait(), timeout=5)
        assert await _until(lambda: bool(heartbeat.failure()))
        failure = heartbeat.failure()
        stop_event.set()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(running), timeout=0.2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        return failure

    assert "has not begun a tick" in _run(_scenario())


def test_an_idle_tier_reports_healthy() -> None:
    stop_event = threading.Event()
    heartbeat = LoopHeartbeat("test", stall_after_sec=0.05)
    runtime = _runtime(_Feed([]), {}, stop_event, heartbeat=heartbeat)

    async def _scenario() -> str:
        running = asyncio.ensure_future(runtime.execute())
        await asyncio.sleep(0.2)
        failure = heartbeat.failure()
        stop_event.set()
        await asyncio.wait_for(running, timeout=5)
        return failure

    assert _run(_scenario()) == ""


def test_the_liveness_bound_is_the_consolidation_lease_window() -> None:
    """Derived from the window rather than stated independently of it."""
    import inspect

    from background_memory.handlers.consolidation import lease_expiry_sec
    from multiuser_config.background_memory import run as tier

    assert "lease_expiry_sec(boot.settings)" in inspect.getsource(tier._serve)
    assert lease_expiry_sec is not None
