"""The loop that consumes memory events and dispatches them to their handler."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Mapping

from background_memory.interfaces import FollowUp, IMemoryEventConsumer, MemoryEvent, MemoryEventHandler
from new_core.identity import OwnerScope
from ouroboros.observability import aef_tracing
from ouroboros.observability.health import LoopHeartbeat

log = logging.getLogger(__name__)

#: One poll's wait — also what bounds shutdown latency and keeps the loop from spinning.
_POLL_TIMEOUT_SEC = 0.2

MAX_WAITING_PER_PAIR = 8

CANCELLED_RELEASE_GRACE_SEC = 5.0


class _Lane:
    """One ownership pair's serialized deliveries: the task serving it and its queue."""

    def __init__(self) -> None:
        self.waiting: deque[MemoryEvent] = deque()
        self.resumed: deque[MemoryEvent] = deque()
        self.task: asyncio.Task[None] | None = None
        self.started_at: float | None = None


class MemoryRuntime:
    """Consumes memory events as a task, dispatching each delivery as a task of its own.

    The loop knows no handler and no event kind: the composition root supplies the
    table, and what to do with an event is the handler's business. A handler may
    ask for one delayed re-delivery, which the loop performs itself rather than
    publishing — a follow-up must not be subject to the transport's drop rule.

    **At most one delivery per ownership pair runs at a time**, and that is correctness
    rather than throttling: the consolidation handler keys its in-flight run by that pair,
    so two deliveries for one pair write to one run object and commit under one cursor the
    compare-and-swap cannot tell apart. The lease that declares the same boundary for the
    deployment does not enforce it inside one process — it is deliberately acquirable by the
    holder that already holds it — so the loop is the guard. Different pairs run freely;
    what bounds them is the process's model-call budget, not a second number here.
    """

    def __init__(
        self,
        consumer: IMemoryEventConsumer,
        *,
        handlers: Mapping[type[MemoryEvent], MemoryEventHandler],
        stop_event: threading.Event,
        heartbeat: LoopHeartbeat,
        drain_timeout_sec: float,
    ) -> None:
        self._consumer = consumer
        self._handlers = dict(handlers)
        self._stop_event = stop_event
        self._heartbeat = heartbeat
        self._drain_timeout_sec = float(drain_timeout_sec)
        #: Outstanding follow-ups as ``{(kind, scope): (due time, event)}``:
        #: at most one delivery per kind and scope can be pending at a time, and
        #: a later request for the same key replaces the earlier one. A follow-up
        #: requested for one scope must not displace one outstanding for another.
        #: Nothing bounds the collection but the handlers' own single-flight — a
        #: handler that runs one unit of work per scope produces at most one
        #: outstanding follow-up per scope. The key is the full scope on purpose:
        #: the loop must not know that consolidation runs are pair-keyed, and a
        #: finer partition can never merge two distinct pending entries.
        self._pending: dict[tuple[type[MemoryEvent], OwnerScope], tuple[float, MemoryEvent]] = {}
        self._lanes: dict[tuple[str, str], _Lane] = {}

    async def execute(self) -> None:
        log.info("Memory runtime loop started")
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(0)
                self._beat()
                due = self._pop_due(time.monotonic())
                if due is not None:
                    self._resume(due)
                    continue
                try:
                    event = await self._consumer.poll(self._poll_timeout())
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("Memory runtime failed to poll for events", exc_info=True)
                    continue
                if event is not None:
                    self._offer(event)
        finally:
            await self._drain()
        log.info("Memory runtime loop stopped")

    # --- liveness -----------------------------------------------------------

    def oldest_delivery_started_at(self) -> float | None:
        """When the oldest delivery still in flight began, or ``None`` when none is."""
        started = [lane.started_at for lane in self._lanes.values() if lane.started_at is not None]
        return min(started) if started else None

    def _beat(self) -> None:
        """The heartbeat is the oldest delivery's age, not the dispatcher's own pass.

        A loop that only started tasks would beat briskly while every delivery it started was
        hung, which is the one failure the signal exists to report. An idle tier beats now and
        is healthy: an idle tier is not a stuck one.
        """
        self._heartbeat.beat(at=self.oldest_delivery_started_at())

    # --- the pair gate ------------------------------------------------------

    @staticmethod
    def _pair(event: MemoryEvent) -> tuple[str, str]:
        return (event.scope.owner_id, event.scope.agent_id)

    def _offer(self, event: MemoryEvent) -> None:
        """Hand one event to its pair's lane, starting the lane when it is idle."""
        pair = self._pair(event)
        lane = self._lanes.get(pair)
        if lane is None:
            self._start_lane(pair, event, resumed=False)
            return
        if len(lane.waiting) >= MAX_WAITING_PER_PAIR:
            log.warning(
                "Memory event dropped: %d already waiting for owner=%s agent=%s",
                len(lane.waiting),
                pair[0],
                pair[1],
            )
            return
        lane.waiting.append(event)

    def _resume(self, event: MemoryEvent) -> None:
        """Hand one follow-up to its pair's lane, ahead of what is merely queued behind it."""
        pair = self._pair(event)
        lane = self._lanes.get(pair)
        if lane is None:
            self._start_lane(pair, event, resumed=True)
            return
        lane.resumed.append(event)

    def _start_lane(self, pair: tuple[str, str], event: MemoryEvent, *, resumed: bool) -> None:
        lane = _Lane()
        (lane.resumed if resumed else lane.waiting).append(event)
        self._lanes[pair] = lane
        lane.task = asyncio.ensure_future(self._serve(pair, lane))

    async def _serve(self, pair: tuple[str, str], lane: _Lane) -> None:
        """One pair's deliveries, one after another; a follow-up is served even while stopping."""
        try:
            while True:
                if lane.resumed:
                    event = lane.resumed.popleft()
                elif lane.waiting and not self._stop_event.is_set():
                    event = lane.waiting.popleft()
                else:
                    break
                lane.started_at = time.monotonic()
                follow_up = await self._deliver(event)
                lane.started_at = None
                if follow_up is not None:
                    self._schedule(pair, lane, follow_up)
        finally:
            if self._lanes.get(pair) is lane:
                del self._lanes[pair]
            abandoned = len(lane.waiting) + len(lane.resumed)
            if abandoned:
                log.warning(
                    "Memory runtime abandoned %d undelivered event(s) for owner=%s agent=%s",
                    abandoned,
                    pair[0],
                    pair[1],
                )

    # --- one delivery -------------------------------------------------------

    async def _deliver(self, event: MemoryEvent) -> FollowUp | None:
        # A follow-up whose delivery cannot be dispatched — no handler is
        # registered for its kind, or the handler raises — is not rescheduled:
        # a failing handler must not be retried into a loop. A handler that
        # holds state between deliveries is responsible for releasing it before
        # it returns, since no further delivery will reach it.
        handler = self._handlers.get(type(event))
        if handler is None:
            log.warning("Memory runtime has no handler for event type %s", type(event).__name__)
            return None
        try:
            with aef_tracing.trace_scope(aef_tracing.ensure_trace_id(event.trace_id)):
                follow_up = await handler(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One lost event, not the end of the loop.
            log.warning("Memory event handler failed for event type %s", type(event).__name__, exc_info=True)
            return None
        return follow_up

    # --- stopping -----------------------------------------------------------

    async def _drain(self) -> None:
        tasks = [lane.task for lane in self._lanes.values() if lane.task is not None]
        if not tasks:
            return
        log.info("Memory runtime draining %d delivery lane(s)", len(tasks))
        deadline = time.monotonic() + self._drain_timeout_sec
        settle = min(CANCELLED_RELEASE_GRACE_SEC, self._drain_timeout_sec / 2.0)
        _done, unfinished = await asyncio.wait(tasks, timeout=max(0.0, deadline - settle - time.monotonic()))
        if unfinished:
            log.warning("Memory runtime cancelled %d delivery lane(s) at the drain bound", len(unfinished))
            for task in unfinished:
                task.cancel()
            await asyncio.wait(unfinished, timeout=max(0.0, deadline - time.monotonic()))
        for task in tasks:
            if task.done() and not task.cancelled() and task.exception() is not None:
                log.warning("A memory delivery lane failed", exc_info=task.exception())

    # --- follow-ups ---------------------------------------------------------

    def _schedule(self, pair: tuple[str, str], lane: _Lane, follow_up: FollowUp) -> None:
        key = (type(follow_up.event), follow_up.event.scope)
        if follow_up.retry_after <= 0 and self._pair(follow_up.event) == pair:
            self._pending.pop(key, None)
            lane.resumed.append(follow_up.event)
            return
        self._pending[key] = (time.monotonic() + max(0.0, follow_up.retry_after), follow_up.event)

    def _pop_due(self, now: float) -> MemoryEvent | None:
        for key, (due_time, event) in self._pending.items():
            if due_time <= now:
                del self._pending[key]
                return event
        return None

    def _poll_timeout(self) -> float:
        if not self._pending:
            return _POLL_TIMEOUT_SEC
        earliest = min(due_time for due_time, _ in self._pending.values())
        return min(_POLL_TIMEOUT_SEC, max(0.0, earliest - time.monotonic()))


__all__ = ["CANCELLED_RELEASE_GRACE_SEC", "MAX_WAITING_PER_PAIR", "MemoryRuntime"]
