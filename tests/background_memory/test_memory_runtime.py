"""The loop: poll, dispatch by table, re-deliver follow-ups — and survive the handler."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import httpx

from background_memory.infra.events.inmemory import InMemoryEventConsumer, InMemoryEventPublisher, MemoryEventQueue
from background_memory.interfaces import (
    ChatMessagePersistedEvent,
    ConsolidationContinuationEvent,
    FollowUp,
    IMemoryEventConsumer,
    MemoryDistillationEvent,
    MemoryEvent,
    MemoryTaskRef,
)
from background_memory.runtime import _POLL_TIMEOUT_SEC, MemoryRuntime
from ouroboros.observability.aef_tracing import (
    TRACE_HEADER_NAME,
    apply_trace_header,
    current_trace_id,
    normalize_trace_id,
)
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.supervisor import memory_heartbeat

#: The drain bound these cases run under: every scripted handler returns at once, so this
#: is only what a case would wait if one did not.
DRAIN_SEC = 5.0

_TRACE_A = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
_TRACE_B = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


@dataclass(frozen=True)
class _UnknownEvent(MemoryEvent):
    """An event kind the table has no entry for."""


@dataclass(frozen=True)
class _FollowUpKindA(MemoryEvent):
    """One follow-up kind with a handler, standing in for one key of the pending map."""


@dataclass(frozen=True)
class _FollowUpKindB(MemoryEvent):
    """A second follow-up kind, so two entries can be pending at once."""


class _Clock:
    """A monotonic clock the test advances, so a delay costs no wall-clock time."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class _ScriptedConsumer(IMemoryEventConsumer):
    """Yields the scripted events, then nothing; counts polls that found nothing."""

    def __init__(
        self,
        events: list[MemoryEvent],
        *,
        stop_event: threading.Event | None = None,
        stop_after_empty: int = 1,
        clock: _Clock | None = None,
    ) -> None:
        self._events = list(events)
        self._stop_event = stop_event
        self._stop_after_empty = stop_after_empty
        self._clock = clock
        self.empty_polls = 0
        self.timeouts: list[float] = []
        #: Set by the driver: the loop stops when its scripted events are spent *and* no
        #: delivery is still in flight, which is what one empty poll meant while deliveries
        #: ran inline and what has to be said out loud now that they do not. A pending
        #: follow-up does not hold it — two cases stop the loop with one outstanding.
        self.runtime: MemoryRuntime | None = None

    async def poll(self, timeout: float = 0.0) -> MemoryEvent | None:
        self.timeouts.append(timeout)
        if self._clock is not None:
            self._clock.advance(timeout)
        if self._events:
            return self._events.pop(0)
        self.empty_polls += 1
        if self._stop_event is not None and self.empty_polls >= self._stop_after_empty and self._quiet():
            self._stop_event.set()
        return None

    async def close(self) -> None:
        return None

    def _quiet(self) -> bool:
        runtime = self.runtime
        return runtime is None or not runtime._lanes


class _FakeHandler:
    """Records what it was handed, and answers with whatever the test scripted."""

    def __init__(self, follow_ups: list[FollowUp | None] | None = None, error: Exception | None = None) -> None:
        self.calls: list[MemoryEvent] = []
        self._follow_ups = list(follow_ups or [])
        self._error = error

    async def handle(self, event: MemoryEvent) -> FollowUp | None:
        self.calls.append(event)
        if self._error is not None:
            raise self._error
        return self._follow_ups.pop(0) if self._follow_ups else None


def _distillation_event(task_id: str = "src1", *, trace_id: str = "") -> MemoryDistillationEvent:
    return MemoryDistillationEvent(
        scope=TEST_SCOPE,
        trace_id=trace_id,
        task=MemoryTaskRef(task_id=task_id, task_type="task", text="do it", recorded_at=1.5),
        response_text="A" * 200,
        llm_trace={"tool_calls": []},
    )


def _trigger(message_id: str = "message-0001") -> ChatMessagePersistedEvent:
    return ChatMessagePersistedEvent(scope=TEST_SCOPE, message_id=message_id)


def _continuation() -> ConsolidationContinuationEvent:
    return ConsolidationContinuationEvent(scope=TEST_SCOPE)


def drive(runtime: MemoryRuntime) -> None:
    """Run the loop to its stop on this test's own event loop."""
    asyncio.get_event_loop().run_until_complete(runtime.execute())


def _run(
    events: list[MemoryEvent],
    handlers: dict[type[MemoryEvent], object],
    *,
    stop_after_empty: int = 1,
    clock: _Clock | None = None,
) -> _ScriptedConsumer:
    stop_event = threading.Event()
    consumer = _ScriptedConsumer(events, stop_event=stop_event, stop_after_empty=stop_after_empty, clock=clock)
    runtime = MemoryRuntime(
        consumer,
        handlers=handlers,  # type: ignore[arg-type]
        stop_event=stop_event,
        heartbeat=memory_heartbeat(),
        drain_timeout_sec=DRAIN_SEC,
    )
    consumer.runtime = runtime
    drive(runtime)
    return consumer


# --- dispatch ---------------------------------------------------------------


def test_two_registered_kinds_reach_two_different_handlers_untouched() -> None:
    """The loop applies no rule of its own: each event arrives as published."""
    distillation, consolidation = _FakeHandler(), _FakeHandler()

    _run(
        [_distillation_event("src-ok"), ChatMessagePersistedEvent(scope=TEST_SCOPE, message_id="message-0001")],
        {
            MemoryDistillationEvent: distillation.handle,
            ChatMessagePersistedEvent: consolidation.handle,
        },
    )

    assert [event.task.task_id for event in distillation.calls] == ["src-ok"]  # type: ignore[attr-defined]
    assert distillation.calls[0].task.recorded_at == 1.5  # type: ignore[attr-defined]
    assert [event.message_id for event in consolidation.calls] == ["message-0001"]  # type: ignore[attr-defined]
    assert consolidation.calls[0].scope == TEST_SCOPE  # type: ignore[attr-defined]


def test_an_unregistered_event_kind_warns_and_the_loop_continues(caplog) -> None:
    caplog.set_level("INFO")
    handler = _FakeHandler()

    _run(
        [_UnknownEvent(scope=TEST_SCOPE), _distillation_event("src-after")],
        {MemoryDistillationEvent: handler.handle},
    )

    assert "no handler for event type _UnknownEvent" in caplog.text
    assert [event.task.task_id for event in handler.calls] == ["src-after"]  # type: ignore[attr-defined]


def test_a_handler_exception_is_logged_and_the_loop_survives(caplog) -> None:
    caplog.set_level("INFO")
    handler = _FakeHandler(error=RuntimeError("kaboom"))

    _run(
        [_distillation_event("src-boom"), _distillation_event("src-next")],
        {MemoryDistillationEvent: handler.handle},
    )

    assert [event.task.task_id for event in handler.calls] == ["src-boom", "src-next"]  # type: ignore[attr-defined]
    assert "kaboom" in caplog.text
    assert "MemoryDistillationEvent" in caplog.text


def test_the_loop_beats_at_the_top_of_every_pass(monkeypatch) -> None:
    stop_event = threading.Event()
    consumer = _ScriptedConsumer([], stop_event=stop_event, stop_after_empty=1)
    heartbeat = memory_heartbeat()
    order: list[str] = []
    monkeypatch.setattr(heartbeat, "beat", lambda at=None: order.append("beat"))
    scripted_poll = consumer.poll

    async def poll(timeout: float):
        order.append("poll")
        return await scripted_poll(timeout)

    monkeypatch.setattr(consumer, "poll", poll)

    drive(MemoryRuntime(consumer, handlers={}, stop_event=stop_event, heartbeat=heartbeat, drain_timeout_sec=DRAIN_SEC))

    assert order[:2] == ["beat", "poll"], "a handler that hangs must leave the last beat ageing behind it"


def test_the_stop_event_ends_the_loop_within_a_poll() -> None:
    stop_event = threading.Event()
    consumer = _ScriptedConsumer([])
    runtime = MemoryRuntime(
        consumer, handlers={}, stop_event=stop_event, heartbeat=memory_heartbeat(), drain_timeout_sec=DRAIN_SEC
    )

    async def _scenario() -> None:
        running = asyncio.ensure_future(runtime.execute())
        await asyncio.sleep(0.05)
        stop_event.set()
        await asyncio.wait_for(running, timeout=3)

    asyncio.get_event_loop().run_until_complete(_scenario())


def test_an_empty_transport_does_not_spin() -> None:
    """The poll's own timeout is what paces the loop; it has no sleep of its own."""
    consumer = _run([], {})

    assert consumer.timeouts == [0.2]


def test_a_polling_failure_does_not_end_the_loop(caplog) -> None:
    caplog.set_level("INFO")
    stop_event = threading.Event()

    class _BrokenThenDone(IMemoryEventConsumer):
        def __init__(self) -> None:
            self.polls = 0

        async def poll(self, timeout: float = 0.0) -> MemoryEvent | None:
            self.polls += 1
            if self.polls == 1:
                raise RuntimeError("transport unavailable")
            stop_event.set()
            return None

        async def close(self) -> None:
            return None

    consumer = _BrokenThenDone()
    drive(
        MemoryRuntime(
            consumer, handlers={}, stop_event=stop_event, heartbeat=memory_heartbeat(), drain_timeout_sec=DRAIN_SEC
        )
    )

    assert consumer.polls == 2
    assert "failed to poll" in caplog.text


# --- follow-ups -------------------------------------------------------------


def test_an_immediate_follow_up_is_delivered_on_the_next_pass() -> None:
    handler = _FakeHandler([FollowUp(_continuation())])
    trigger = _trigger()

    consumer = _run(
        [trigger],
        {
            ChatMessagePersistedEvent: handler.handle,
            ConsolidationContinuationEvent: handler.handle,
        },
    )

    assert handler.calls == [trigger, _continuation()]
    # The continuation cost no poll of its own: the loop delivered it directly.
    assert consumer.timeouts == [0.2, 0.2]


def test_a_delayed_follow_up_waits_out_its_delay_without_spinning(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr("background_memory.runtime.time", SimpleNamespace(monotonic=clock.monotonic))
    trigger = _trigger()
    stop_event = threading.Event()
    calls: list[MemoryEvent] = []

    async def handle(event: MemoryEvent) -> FollowUp | None:
        calls.append(event)
        if isinstance(event, ConsolidationContinuationEvent):
            stop_event.set()  # the delayed delivery arrived; nothing more to watch
            return None
        return FollowUp(_continuation(), retry_after=5.0)

    consumer = _ScriptedConsumer([trigger], clock=clock)
    drive(
        MemoryRuntime(
            consumer,
            handlers={ChatMessagePersistedEvent: handle, ConsolidationContinuationEvent: handle},
            stop_event=stop_event,
            heartbeat=memory_heartbeat(),
            drain_timeout_sec=DRAIN_SEC,
        )
    )

    # Waited the delay out one bounded poll at a time, then delivered it. The poll
    # count is the delay over the poll timeout, give or take a float's last bit.
    assert calls == [trigger, _continuation()]
    assert 24 <= consumer.empty_polls <= 26
    assert all(0 < timeout <= 0.2 for timeout in consumer.timeouts)


def test_a_pending_follow_up_is_not_delivered_before_its_delay(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr("background_memory.runtime.time", SimpleNamespace(monotonic=clock.monotonic))
    handler = _FakeHandler([FollowUp(_continuation(), retry_after=5.0)])
    trigger = _trigger()

    # Ten empty polls is two simulated seconds — well short of the delay.
    _run(
        [trigger],
        {
            ChatMessagePersistedEvent: handler.handle,
            ConsolidationContinuationEvent: handler.handle,
        },
        stop_after_empty=10,
        clock=clock,
    )

    assert handler.calls == [trigger]


def test_shutdown_during_a_pending_follow_up_ends_the_loop_without_delivering_it() -> None:
    handler = _FakeHandler([FollowUp(_continuation(), retry_after=5.0)])
    trigger = _trigger()

    _run(
        [trigger],
        {
            ChatMessagePersistedEvent: handler.handle,
            ConsolidationContinuationEvent: handler.handle,
        },
    )

    assert handler.calls == [trigger]


def test_a_follow_up_is_delivered_while_the_transport_is_at_capacity() -> None:
    """Follow-ups never enter the queue, so a full transport cannot drop one."""
    shared = MemoryEventQueue(1)
    publisher, consumer_end = InMemoryEventPublisher(shared), InMemoryEventConsumer(shared)
    trigger = _trigger()
    publisher.publish(trigger)
    publisher.publish(_trigger("message-0002"))  # dropped: full
    handler = _FakeHandler([FollowUp(_continuation())])
    stop_event = threading.Event()
    runtime = MemoryRuntime(
        consumer_end,
        handlers={
            ChatMessagePersistedEvent: handler.handle,
            ConsolidationContinuationEvent: handler.handle,
        },
        stop_event=stop_event,
        heartbeat=memory_heartbeat(),
        drain_timeout_sec=DRAIN_SEC,
    )

    async def _scenario() -> None:
        running = asyncio.ensure_future(runtime.execute())
        deadline = time.monotonic() + 5
        while len(handler.calls) < 2 and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(running, timeout=5)

    asyncio.get_event_loop().run_until_complete(_scenario())

    assert handler.calls == [trigger, _continuation()]


def test_follow_ups_for_different_keys_are_both_delivered_each_after_its_own_delay(
    background_loop, monkeypatch
) -> None:
    """One key's follow-up never displaces another's; each waits out its own delay."""
    clock = _Clock()
    monkeypatch.setattr("background_memory.runtime.time", SimpleNamespace(monotonic=clock.monotonic))
    start = clock.now
    delivered: list[MemoryEvent] = []
    delivery_times: dict[type[MemoryEvent], float] = {}
    stop_event = threading.Event()

    async def handle(event: MemoryEvent) -> FollowUp | None:
        delivered.append(event)
        if isinstance(event, ChatMessagePersistedEvent):
            return FollowUp(_FollowUpKindA(scope=TEST_SCOPE), retry_after=1.0)
        if isinstance(event, MemoryDistillationEvent):
            return FollowUp(_FollowUpKindB(scope=TEST_SCOPE), retry_after=2.0)
        if isinstance(event, (_FollowUpKindA, _FollowUpKindB)):
            delivery_times[type(event)] = clock.now
            if len(delivery_times) == 2:
                stop_event.set()
        return None

    consumer = _ScriptedConsumer(
        [_trigger("m1"), _distillation_event("src1")],
        clock=clock,
        stop_after_empty=1000,
    )
    drive(
        MemoryRuntime(
            consumer,
            handlers={
                ChatMessagePersistedEvent: handle,
                MemoryDistillationEvent: handle,
                _FollowUpKindA: handle,
                _FollowUpKindB: handle,
            },
            stop_event=stop_event,
            heartbeat=memory_heartbeat(),
            drain_timeout_sec=DRAIN_SEC,
        )
    )

    assert delivered == [
        _trigger("m1"),
        _distillation_event("src1"),
        _FollowUpKindA(scope=TEST_SCOPE),
        _FollowUpKindB(scope=TEST_SCOPE),
    ]
    # Each waited out its own delay before delivery, and the earlier one came first.
    assert delivery_times[_FollowUpKindA] >= start + 1.0
    assert delivery_times[_FollowUpKindB] >= start + 2.0
    assert delivery_times[_FollowUpKindA] < delivery_times[_FollowUpKindB]


def test_a_repeated_follow_up_request_for_one_key_does_not_accumulate(monkeypatch) -> None:
    """Two requests for the same key leave one outstanding delivery, not two."""
    clock = _Clock()
    monkeypatch.setattr("background_memory.runtime.time", SimpleNamespace(monotonic=clock.monotonic))
    delivered: list[MemoryEvent] = []
    stop_event = threading.Event()

    async def handle(event: MemoryEvent) -> FollowUp | None:
        delivered.append(event)
        if isinstance(event, (ChatMessagePersistedEvent, MemoryDistillationEvent)):
            return FollowUp(_FollowUpKindA(scope=TEST_SCOPE), retry_after=5.0)
        if isinstance(event, _FollowUpKindA):
            stop_event.set()
        return None

    consumer = _ScriptedConsumer(
        [_trigger("m1"), _distillation_event("src1")],
        clock=clock,
        stop_after_empty=1000,
    )
    drive(
        MemoryRuntime(
            consumer,
            handlers={
                ChatMessagePersistedEvent: handle,
                MemoryDistillationEvent: handle,
                _FollowUpKindA: handle,
            },
            stop_event=stop_event,
            heartbeat=memory_heartbeat(),
            drain_timeout_sec=DRAIN_SEC,
        )
    )

    assert len([event for event in delivered if isinstance(event, _FollowUpKindA)]) == 1


def test_the_earliest_pending_follow_up_bounds_the_wait(monkeypatch) -> None:
    """While two follow-ups are pending, the poll waits at most until the earlier one."""
    clock = _Clock()
    monkeypatch.setattr("background_memory.runtime.time", SimpleNamespace(monotonic=clock.monotonic))
    stop_event = threading.Event()

    async def handle(event: MemoryEvent) -> FollowUp | None:
        if isinstance(event, ChatMessagePersistedEvent):
            return FollowUp(_FollowUpKindA(scope=TEST_SCOPE), retry_after=0.1)
        if isinstance(event, MemoryDistillationEvent):
            return FollowUp(_FollowUpKindB(scope=TEST_SCOPE), retry_after=5.0)
        if isinstance(event, (_FollowUpKindA, _FollowUpKindB)):
            stop_event.set()
        return None

    consumer = _ScriptedConsumer(
        [_trigger("m1"), _distillation_event("src1")],
        clock=clock,
        stop_after_empty=1000,
    )
    drive(
        MemoryRuntime(
            consumer,
            handlers={
                ChatMessagePersistedEvent: handle,
                MemoryDistillationEvent: handle,
                _FollowUpKindA: handle,
                _FollowUpKindB: handle,
            },
            stop_event=stop_event,
            heartbeat=memory_heartbeat(),
            drain_timeout_sec=DRAIN_SEC,
        )
    )

    # The poll taken while both were pending was cut to the earlier delay (0.1s),
    # not left at the poll cap (0.2s) — the later entry must not bound the wait.
    assert consumer.timeouts[1] <= 0.1 + 1e-9
    assert all(timeout <= _POLL_TIMEOUT_SEC + 1e-9 for timeout in consumer.timeouts)


# --- the operation the delivery belongs to -----------------------------------


class _TraceRecordingHandler:
    """Notes the operation identifier that was active while it ran."""

    def __init__(self, error: Exception | None = None) -> None:
        self.seen: list[str] = []
        self._error = error

    def handle(self, event: MemoryEvent) -> FollowUp | None:
        self.seen.append(current_trace_id())
        if self._error is not None:
            raise self._error
        return None


def test_a_delivery_runs_under_the_operation_the_event_names() -> None:
    """The loop's own thread sees no publisher context, so it reads the event."""
    handler = _TraceRecordingHandler()

    _run(
        [_distillation_event("src1", trace_id=_TRACE_A)],
        {MemoryDistillationEvent: handler.handle},
    )

    assert handler.seen == [_TRACE_A]


def test_an_event_naming_no_operation_still_runs_under_one() -> None:
    """Running unidentified would send outbound calls with no header and no error."""
    handler = _TraceRecordingHandler()

    _run(
        [_distillation_event("src1"), _distillation_event("src2")],
        {MemoryDistillationEvent: handler.handle},
    )

    assert all(normalize_trace_id(seen) == seen and seen for seen in handler.seen)
    # Two deliveries with nothing to correlate them are two operations, not one.
    assert handler.seen[0] != handler.seen[1]


def test_a_failing_delivery_does_not_leak_its_operation() -> None:
    failing, recording = _TraceRecordingHandler(error=RuntimeError("kaboom")), _TraceRecordingHandler()

    _run(
        [_distillation_event("src-boom", trace_id=_TRACE_A), _trigger("message-0001")],
        {MemoryDistillationEvent: failing.handle, ChatMessagePersistedEvent: recording.handle},
    )

    assert failing.seen == [_TRACE_A]
    assert recording.seen[0] != _TRACE_A
    assert current_trace_id() == ""


def test_the_loop_reads_the_identifier_off_the_base_type() -> None:
    """Any kind, including one the loop has never seen, is scoped the same way."""
    handler = _TraceRecordingHandler()

    _run(
        [_FollowUpKindA(scope=TEST_SCOPE, trace_id=_TRACE_B)],
        {_FollowUpKindA: handler.handle},
    )

    assert handler.seen == [_TRACE_B]


def test_a_follow_up_is_delivered_under_the_operation_it_names() -> None:
    """A run handing itself back stays one operation across deliveries."""
    seen: list[str] = []

    async def handle(event: MemoryEvent) -> FollowUp | None:
        seen.append(current_trace_id())
        if isinstance(event, ChatMessagePersistedEvent):
            return FollowUp(ConsolidationContinuationEvent(scope=TEST_SCOPE, trace_id=current_trace_id()))
        return None

    _run(
        [_trigger(), _trigger("message-0002")],
        {ChatMessagePersistedEvent: handle, ConsolidationContinuationEvent: handle},
    )

    # trigger, its continuation, then a second unrelated trigger: the first two agree.
    assert seen[0] == seen[1]
    assert seen[2] != seen[0]


def test_an_outbound_request_built_inside_a_delivery_carries_the_header() -> None:
    """The production defect, in one assertion.

    Context, not the header, is what the other tests here pin, and the header is what was
    actually missing: ``apply_trace_header`` — the function every httpx client's hook calls —
    adds nothing when no operation is active, so a delivery that runs unscoped sends an
    uncorrelated request and nothing anywhere fails.
    """
    sent: list[str | None] = []

    async def handle(event: MemoryEvent) -> FollowUp | None:
        request = httpx.Request("POST", "http://llm.invalid/v1/chat/completions")
        apply_trace_header(request)
        sent.append(request.headers.get(TRACE_HEADER_NAME))
        return None

    _run(
        [_distillation_event("src1", trace_id=_TRACE_A)],
        {MemoryDistillationEvent: handle},
    )

    assert sent == [_TRACE_A]
