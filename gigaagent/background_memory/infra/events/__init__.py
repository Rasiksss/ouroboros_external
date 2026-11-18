"""The one object that decides which transport this process publishes through."""

import logging

from background_memory.infra.events.inmemory import InMemoryEventPublisher, MemoryEventQueue
from background_memory.interfaces import (
    ChatMessagePersistedEvent,
    IMemoryEventPublisher,
    NullMemoryEventPublisher,
)
from new_core.identity import OwnerScope
from ouroboros.observability import aef_tracing

log = logging.getLogger(__name__)


# The consumer half has no holder and is not getting one: the loop is handed its consumer
# by the root that starts it (`server.py`, `multiuser_config/background_memory/`), so there
# is no code that cannot be given one as an argument. What this holder exists for is the
# producer half, which is read from a task graph nothing carries a publisher into.
class MemoryEventPublisherFactory:
    """Holds the process's publisher, built once by the composition root.

    Code that cannot be handed the publisher as an argument reads it here. Until a
    transport is initialized the publisher is the null one, so a process that never
    configures background memory still runs.
    """

    def __init__(self) -> None:
        self._memory_publisher: IMemoryEventPublisher = NullMemoryEventPublisher()

    def initialize_inmemory(self, queue: MemoryEventQueue) -> None:
        self._memory_publisher = InMemoryEventPublisher(queue)

    def initialize_broker(self, publisher: IMemoryEventPublisher) -> None:
        """Install an already-built broker publisher.

        Takes the publisher rather than a client and a stream name on purpose: this module
        is imported by every process that builds a task graph, so constructing the backend
        here would put ``import redis`` on every single-process start — the new dependency
        is meant to be a wheel, not a code path. The root that names the broker is the root
        that builds the client (`ouroboros/app/broker_composition.py`).
        """
        self._memory_publisher = publisher

    @property
    def memory_publisher(self) -> IMemoryEventPublisher:
        return self._memory_publisher


memory_event_publisher_factory = MemoryEventPublisherFactory()


def announce_chat_message_persisted(
    publisher: IMemoryEventPublisher,
    scope: OwnerScope,
    message_id: str,
) -> None:
    """Tell background memory a row landed; a failed announcement never fails the write.

    Every persisted row counts toward the backlog, so every one announces itself; whether
    that backlog is worth summarizing is decided by the consolidation handler, on the
    memory loop. The publisher is an argument rather than a factory read: a writer that
    can be handed one is handed one. The scope is the one the write used, passed through
    rather than minted here — unlike ``append_chat_message``, an announcement writes no
    metadata that needs the raw chat id, so none is carried beside the scope.
    """
    try:
        publisher.publish(
            ChatMessagePersistedEvent(
                scope=scope,
                # The operation that wrote the row, carried to the memory loop, which
                # has no view of the writer's context.
                trace_id=aef_tracing.current_trace_id(),
                message_id=str(message_id or ""),
            )
        )
    except Exception:
        log.warning("Failed to publish the chat message persisted event", exc_info=True)


__all__ = [
    "MemoryEventPublisherFactory",
    "announce_chat_message_persisted",
    "memory_event_publisher_factory",
]
