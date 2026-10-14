"""The seam: two interfaces, messages under one base, and the null publisher."""

from __future__ import annotations

import dataclasses

import pytest

from background_memory.interfaces import (
    ChatMessagePersistedEvent,
    ConsolidationContinuationEvent,
    IMemoryEventConsumer,
    IMemoryEventPublisher,
    MemoryDistillationEvent,
    MemoryEvent,
    MemoryTaskRef,
    NullMemoryEventPublisher,
)
from tests.helpers.memory_event_conformance import poll
from tests.helpers.shared import TEST_SCOPE


def test_a_publisher_omitting_publish_cannot_be_instantiated() -> None:
    class Incomplete(IMemoryEventPublisher):
        """publish intentionally omitted — the class must stay abstract."""

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_a_consumer_omitting_poll_cannot_be_instantiated() -> None:
    class Incomplete(IMemoryEventConsumer):
        """poll intentionally omitted — the class must stay abstract."""

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_a_consumer_omitting_close_cannot_be_instantiated() -> None:
    class Unreleasable(IMemoryEventConsumer):
        """close intentionally omitted — what a consumer holds on the broker is its own to free."""

        async def poll(self, timeout: float = 0.0):
            return None

    with pytest.raises(TypeError):
        Unreleasable()  # type: ignore[abstract]


def test_the_null_publisher_accepts_and_discards() -> None:
    publisher = NullMemoryEventPublisher()
    assert publisher.publish(MemoryDistillationEvent(scope=TEST_SCOPE, task=MemoryTaskRef(task_id="t1"))) is None


def test_every_message_derives_from_one_base() -> None:
    """``poll`` is typed to the base, so a second event kind needs no interface change."""
    assert issubclass(MemoryDistillationEvent, MemoryEvent)


def test_messages_are_frozen_named_types_not_dictionaries() -> None:
    event = MemoryDistillationEvent(
        scope=TEST_SCOPE, task=MemoryTaskRef(task_id="t1", task_type="task"), response_text="body"
    )
    assert event.task.task_id == "t1"
    # FrozenInstanceError is a dataclasses detail; what matters is that it raises.
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.response_text = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.task.task_id = "other"  # type: ignore[misc]


def test_poll_defaults_to_non_blocking() -> None:
    """The default is what makes a non-blocking read the caller's plain ``poll()``."""
    import inspect

    assert inspect.signature(IMemoryEventConsumer.poll).parameters["timeout"].default == 0.0


# --- identity on the base ----------------------------------------------------


def test_a_message_cannot_be_constructed_without_a_scope() -> None:
    """A kind added later cannot forget to say whose it is."""
    with pytest.raises(TypeError):
        ChatMessagePersistedEvent(message_id="message-0001")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ConsolidationContinuationEvent()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        MemoryDistillationEvent(task=MemoryTaskRef(task_id="t1"))  # type: ignore[call-arg]


def test_identity_is_declared_once_on_the_base() -> None:
    """No kind declares an identity field of its own; the scope is the base's field."""
    assert "scope" in MemoryEvent.__dataclass_fields__
    assert MemoryEvent.__dataclass_fields__["scope"].default is dataclasses.MISSING
    for kind in (MemoryDistillationEvent, ChatMessagePersistedEvent, ConsolidationContinuationEvent):
        own = set(kind.__dataclass_fields__) - set(MemoryEvent.__dataclass_fields__)
        assert not own & {"scope", "chat_id", "owner_id", "agent_id"}, kind


def test_the_operation_is_declared_once_on_the_base_and_defaults_to_empty() -> None:
    """A message crosses a thread; a context variable does not, so it travels as a field."""
    assert MemoryEvent.__dataclass_fields__["trace_id"].default == ""
    for kind in (MemoryDistillationEvent, ChatMessagePersistedEvent, ConsolidationContinuationEvent):
        own = set(kind.__dataclass_fields__) - set(MemoryEvent.__dataclass_fields__)
        assert "trace_id" not in own, kind
        # Naming only the scope is enough: what an absent operation means is the
        # consumer's decision, not a reason a publisher cannot build the message.
        assert kind(scope=TEST_SCOPE).trace_id == ""


def test_a_runtime_internal_kind_carries_a_scope_too() -> None:
    """A re-delivery names the work it resumes rather than assuming there is one."""
    event = ConsolidationContinuationEvent(scope=TEST_SCOPE)
    assert event.scope == TEST_SCOPE


def test_a_published_event_round_trips_through_the_transport_unchanged() -> None:
    """``OwnerScope`` equality is what makes a polled event equal its published one."""

    from background_memory.infra.events.inmemory import InMemoryEventConsumer, InMemoryEventPublisher, MemoryEventQueue

    shared = MemoryEventQueue(4)
    event = MemoryDistillationEvent(scope=TEST_SCOPE, task=MemoryTaskRef(task_id="t1"))

    InMemoryEventPublisher(shared).publish(event)

    assert poll(InMemoryEventConsumer(shared)) == event


def test_consuming_is_awaitable_and_publishing_is_not() -> None:
    """The asymmetry is the decision: a producer is in a thread, a consumer is the loop."""
    import inspect

    from background_memory.interfaces import IMemoryEventConsumer, IMemoryEventPublisher

    assert inspect.iscoroutinefunction(IMemoryEventConsumer.poll)
    assert not inspect.iscoroutinefunction(IMemoryEventPublisher.publish)
