"""The in-memory transport: round-trip, timeout, bounded overflow, ordering."""

from __future__ import annotations

import ast
import pathlib
import time
from collections.abc import Sequence

from background_memory.infra.events import inmemory as inmemory_mod
from background_memory.infra.events.inmemory import (
    MAX_PENDING_EVENTS,
    InMemoryEventConsumer,
    InMemoryEventPublisher,
    MemoryEventQueue,
)
from background_memory.interfaces import (
    ChatMessagePersistedEvent,
    IMemoryEventConsumer,
    IMemoryEventPublisher,
    MemoryDistillationEvent,
    MemoryEvent,
)
from tests.helpers.memory_event_conformance import MemoryEventConformance, MemoryEventPair, distillation_event, poll
from tests.helpers.shared import TEST_SCOPE


def _pair(maxsize: int = 8) -> tuple[InMemoryEventPublisher, InMemoryEventConsumer]:
    shared = MemoryEventQueue(maxsize)
    return InMemoryEventPublisher(shared), InMemoryEventConsumer(shared)


class TestInMemoryEventConformance(MemoryEventConformance):
    _queue: MemoryEventQueue

    def make_publisher_and_consumer(self) -> MemoryEventPair:
        self._queue = MemoryEventQueue(MAX_PENDING_EVENTS)
        return InMemoryEventPublisher(self._queue), InMemoryEventConsumer(self._queue)

    def second_consumer(self, consumer: IMemoryEventConsumer) -> IMemoryEventConsumer:
        return InMemoryEventConsumer(self._queue)

    def make_group_after_history(self, history: Sequence[MemoryEvent]) -> MemoryEventPair | None:
        return None

    def break_broker(self, publisher: IMemoryEventPublisher, consumer: IMemoryEventConsumer) -> bool:
        return False

    def inject_raw_record(self, consumer: IMemoryEventConsumer, payload: bytes) -> bool:
        return False

    def serializes_events(self) -> bool:
        return False


def test_the_published_object_itself_is_handed_back() -> None:
    publisher, consumer = _pair()
    event = distillation_event()

    publisher.publish(event)
    polled = poll(consumer)

    assert polled is event
    assert isinstance(polled, MemoryDistillationEvent)
    assert polled.task == event.task


def test_a_read_with_no_timeout_returns_within_a_tenth_of_a_second() -> None:
    _, consumer = _pair()

    started = time.monotonic()
    assert poll(consumer) is None
    assert time.monotonic() - started < 0.1


def test_a_read_with_a_short_timeout_returns_within_a_second() -> None:
    _, consumer = _pair()

    started = time.monotonic()
    assert poll(consumer, 0.2) is None
    assert 0.15 <= time.monotonic() - started < 1.0


def test_publishing_into_a_full_transport_drops_the_newest(caplog) -> None:
    caplog.set_level("INFO")
    publisher, consumer = _pair(maxsize=2)

    publisher.publish(distillation_event("first"))
    publisher.publish(distillation_event("second"))
    publisher.publish(distillation_event("overflow"))

    assert "cap=2" in caplog.text
    assert "scope=" in caplog.text

    # Ordering of what was already queued survives, and nothing was evicted.
    assert [poll(consumer).task.task_id for _ in range(2)] == ["first", "second"]  # type: ignore[union-attr]
    assert poll(consumer) is None


def test_a_kind_without_a_source_task_still_warns_when_dropped(caplog) -> None:
    """The drop names the scope, which every kind carries."""
    caplog.set_level("INFO")
    publisher, _ = _pair(maxsize=1)

    publisher.publish(ChatMessagePersistedEvent(scope=TEST_SCOPE, message_id="message-0001"))
    publisher.publish(ChatMessagePersistedEvent(scope=TEST_SCOPE, message_id="message-0002"))  # must not raise

    assert "cap=1" in caplog.text
    assert "scope=" in caplog.text


def test_the_shared_capacity_leaves_room_for_both_kinds() -> None:
    """Sized for a burst of triggers during one run, with distillation headroom left."""
    publisher, consumer = _pair(maxsize=MAX_PENDING_EVENTS)

    for index in range(MAX_PENDING_EVENTS - 1):
        publisher.publish(ChatMessagePersistedEvent(scope=TEST_SCOPE, message_id=f"message-{index:04d}"))
    publisher.publish(distillation_event("still-fits"))

    polled = [poll(consumer) for _ in range(MAX_PENDING_EVENTS)]
    assert isinstance(polled[-1], MemoryDistillationEvent)
    assert polled[-1].task.task_id == "still-fits"  # type: ignore[union-attr]


def test_queued_events_are_delivered_in_order() -> None:
    publisher, consumer = _pair()

    for index in range(5):
        publisher.publish(distillation_event(f"src{index}"))

    assert [poll(consumer).task.task_id for _ in range(5)] == [f"src{i}" for i in range(5)]  # type: ignore[union-attr]


def test_the_transport_does_not_reach_the_handler() -> None:
    """A transport that imported the handler would not be dumb."""
    source = pathlib.Path(inmemory_mod.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    assert not [name for name in imported if "handlers" in name or "runtime" in name], imported
