"""The factory the task graph reads its publisher from."""

from __future__ import annotations

import pytest

from background_memory.infra.events import (
    MemoryEventPublisherFactory,
    announce_chat_message_persisted,
    memory_event_publisher_factory,
)
from background_memory.infra.events.inmemory import InMemoryEventConsumer, InMemoryEventPublisher, MemoryEventQueue
from background_memory.interfaces import (
    ChatMessagePersistedEvent,
    IMemoryEventConsumer,
    IMemoryEventPublisher,
    MemoryDistillationEvent,
    MemoryEvent,
    MemoryTaskRef,
    NullMemoryEventPublisher,
)
from ouroboros.observability.aef_tracing import trace_scope
from tests.helpers.memory_event_conformance import poll, reset_memory_event_publisher
from tests.helpers.shared import TEST_SCOPE


@pytest.fixture(autouse=True)
def _clean_factory():
    reset_memory_event_publisher()
    yield
    reset_memory_event_publisher()


def _event(task_id: str = "t1") -> MemoryDistillationEvent:
    return MemoryDistillationEvent(scope=TEST_SCOPE, task=MemoryTaskRef(task_id=task_id))


def test_the_default_publisher_accepts_and_delivers_nothing() -> None:
    """A process that never initializes a transport still runs."""
    factory = MemoryEventPublisherFactory()

    publisher = factory.memory_publisher

    assert isinstance(publisher, NullMemoryEventPublisher)
    assert publisher.publish(_event()) is None


def test_the_initialized_publisher_is_what_callers_read_back() -> None:
    events = MemoryEventQueue(4)
    factory = MemoryEventPublisherFactory()

    factory.initialize_inmemory(events)

    publisher = factory.memory_publisher
    assert isinstance(publisher, InMemoryEventPublisher)
    assert factory.memory_publisher is publisher  # one publisher, not one per read

    publisher.publish(_event("t-read-back"))
    polled = poll(InMemoryEventConsumer(events))
    assert isinstance(polled, MemoryDistillationEvent)
    assert polled.task.task_id == "t-read-back"


def test_a_broker_publisher_is_installed_as_it_is_built() -> None:
    """The second initializer takes the built publisher, not a client and a stream name.

    This module is imported by every process that builds a task graph, so constructing a
    backend here would put ``import redis`` on every single-process start.
    """
    import inspect

    from background_memory.infra.events.redis_transport import RedisMemoryEventPublisher

    built = RedisMemoryEventPublisher(client=object(), stream="s:memory-events", max_entries=8)
    factory = MemoryEventPublisherFactory()

    factory.initialize_broker(built)

    assert factory.memory_publisher is built
    assert list(inspect.signature(factory.initialize_broker).parameters) == ["publisher"]


def test_a_second_initializer_leaves_the_default_alone() -> None:
    """A process that configures nothing still runs — the consuming tier is one of them."""
    factory = MemoryEventPublisherFactory()

    assert {name for name in dir(factory) if name.startswith("initialize_")} == {
        "initialize_inmemory",
        "initialize_broker",
    }
    assert isinstance(factory.memory_publisher, NullMemoryEventPublisher)


def test_the_publisher_it_hands_out_cannot_consume() -> None:
    factory = MemoryEventPublisherFactory()
    factory.initialize_inmemory(MemoryEventQueue(4))

    assert not isinstance(factory.memory_publisher, IMemoryEventConsumer)
    assert not hasattr(factory.memory_publisher, "poll")


def test_the_reset_helper_restores_the_null_publisher() -> None:
    memory_event_publisher_factory.initialize_inmemory(MemoryEventQueue(4))

    reset_memory_event_publisher()

    assert isinstance(memory_event_publisher_factory.memory_publisher, NullMemoryEventPublisher)


# -- the one announcement every writer of a chat row makes ---------------------


def test_announcing_a_persisted_row_publishes_the_event() -> None:
    events = MemoryEventQueue(4)

    announce_chat_message_persisted(InMemoryEventPublisher(events), TEST_SCOPE, "message-0001")

    published = poll(InMemoryEventConsumer(events))
    assert isinstance(published, ChatMessagePersistedEvent)
    assert (published.scope, published.message_id) == (TEST_SCOPE, "message-0001")


def test_a_publisher_that_raises_never_fails_the_write(caplog) -> None:
    """The row is already stored; losing its announcement must not undo the write."""

    class _Broken(IMemoryEventPublisher):
        def publish(self, event: MemoryEvent) -> None:
            raise RuntimeError("broker is down")

    with caplog.at_level("WARNING", logger="background_memory.infra.events"):
        announce_chat_message_persisted(_Broken(), TEST_SCOPE, "message-0002")

    assert [record.levelname for record in caplog.records] == ["WARNING"]


def test_the_announcement_takes_its_publisher_rather_than_reading_the_factory() -> None:
    """A writer that can be handed a publisher is handed one; the factory is the fallback."""
    import inspect

    parameters = list(inspect.signature(announce_chat_message_persisted).parameters)
    assert parameters[:2] == ["publisher", "scope"]

    events = MemoryEventQueue(4)
    memory_event_publisher_factory.initialize_inmemory(events)

    announce_chat_message_persisted(NullMemoryEventPublisher(), TEST_SCOPE, "message-0003")

    assert events.empty(), "the factory's publisher was used instead of the given one"


def test_the_announcement_carries_the_operation_that_wrote_the_row() -> None:
    """The memory loop has no view of the writer's context, so it travels as a field."""
    events = MemoryEventQueue(4)
    trace_id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    with trace_scope(trace_id):
        announce_chat_message_persisted(InMemoryEventPublisher(events), TEST_SCOPE, "message-0004")

    published = poll(InMemoryEventConsumer(events))
    assert published is not None and published.trace_id == trace_id


def test_an_announcement_outside_an_operation_names_none() -> None:
    events = MemoryEventQueue(4)

    announce_chat_message_persisted(InMemoryEventPublisher(events), TEST_SCOPE, "message-0005")

    published = poll(InMemoryEventConsumer(events))
    assert published is not None and published.trace_id == ""
