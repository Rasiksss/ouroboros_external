"""The transport seam for background memory work: two interfaces and the messages.

Publisher and consumer are separate ABCs, as a Kafka producer and consumer are
separate objects — neither side's dependency carries the other side's method.
Messages are frozen dataclasses under one :class:`MemoryEvent` base, so a second
event kind is a new subclass rather than a change to either interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from new_core.identity import OwnerScope


@dataclass(frozen=True)
class MemoryTaskRef:
    """What a publisher knows about the task an event describes.

    ``recorded_at`` is the source task's terminal timestamp. Foreground completion
    stamps it while publishing; durable reconciliation reuses the persisted
    ``finished_at``, so delivery retries never change memory chronology.
    """

    job_id: str = ""
    task_id: str = ""
    task_type: str = ""
    text: str = ""
    recorded_at: float = 0.0


@dataclass(frozen=True)
class MemoryEvent:
    """Base for every message that crosses the transport.

    ``scope`` names whose work the message describes. It is required on the base
    and declared nowhere else: a kind added later cannot forget to say whose it
    is, and a transport can read identity off any message without naming a kind —
    a partition key is the one thing a broker needs and the one thing these
    interfaces must not learn a kind to obtain.

    ``trace_id`` names the operation that caused it, and is on the base for the
    same reason. A message crosses a thread, and a context variable does not, so
    the operation travels as a field or not at all. It is not identity: whose
    work this is is the scope, which operation caused it is this, and neither
    substitutes for the other.

    Unlike the scope it defaults to empty, because a message may describe work no
    operation triggered. What that means is the consumer's decision — the loop
    generates one rather than dispatching unidentified — not a reason a publisher
    should be unable to construct the message.
    """

    scope: OwnerScope
    trace_id: str = ""


@dataclass(frozen=True)
class MemoryDistillationEvent(MemoryEvent):
    """A completed task offered to distillation, with no eligibility rule applied."""

    task: MemoryTaskRef = field(default_factory=MemoryTaskRef)
    response_text: str = ""
    llm_trace: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatMessagePersistedEvent(MemoryEvent):
    """An agent message landed in chat history, offered raw with no threshold applied.

    It names the message and nothing more: what a backlog is worth summarizing is
    the consolidation handler's business, not the publisher's.
    """

    message_id: str = ""


@dataclass(frozen=True)
class ConsolidationContinuationEvent(MemoryEvent):
    """Runtime-internal: a run asking the loop to hand it back.

    Never published — it exists only as a :class:`FollowUp` payload, so a resumed
    run is distinguishable from a fresh trigger. Publishing one would let the
    transport drop it, and a Kafka replay resurrect a half-finished run. It
    carries the scope of the run it resumes rather than assuming there is one.
    """


@dataclass(frozen=True)
class FollowUp:
    """A handler's request that one event be delivered to it again.

    The loop delivers it directly rather than publishing it, so a follow-up is subject to
    neither the transport's capacity nor the pair's lane cap.
    ``retry_after`` is the delay before delivery: zero means the next pass.
    """

    event: MemoryEvent
    retry_after: float = 0.0


MemoryEventHandler = Callable[[Any], Awaitable[FollowUp | None]]


class IMemoryEventPublisher(ABC):
    """The producer half of the seam: what a completed task is reported through."""

    @abstractmethod
    def publish(self, event: MemoryEvent) -> None:
        """Hand one event to the transport. Never blocks the calling thread."""


class IMemoryEventConsumer(ABC):
    @abstractmethod
    async def poll(self, timeout: float = 0.0) -> MemoryEvent | None:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass


class IForegroundPressure(ABC):
    @abstractmethod
    async def is_pending(self) -> bool:
        pass


class NullMemoryEventPublisher(IMemoryEventPublisher):
    """Accepts every event and delivers none — the factory's default.

    A process that never initializes a transport still runs, dropping its events.
    """

    def publish(self, event: MemoryEvent) -> None:
        return None


__all__ = [
    "ChatMessagePersistedEvent",
    "ConsolidationContinuationEvent",
    "FollowUp",
    "IForegroundPressure",
    "IMemoryEventConsumer",
    "IMemoryEventPublisher",
    "MemoryDistillationEvent",
    "MemoryEvent",
    "MemoryEventHandler",
    "MemoryTaskRef",
    "NullMemoryEventPublisher",
]
