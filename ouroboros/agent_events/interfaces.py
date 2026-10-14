"""The transport seam a run reports through: two interfaces and the messages.

Publisher and consumer are separate ABCs, as a Kafka producer and consumer are separate
objects — neither side's dependency carries the other side's method. Messages are frozen
dataclasses under one :class:`AgentEvent` base, so a second kind is a new subclass rather
than a change to either interface.

Nothing on this seam is a fact the run itself must record. A terminal status and a lease
are written by the run, directly, and only reported here: once the consumer can be a
different process, a write it performs on the run's behalf is a fact recorded by a
component that did not witness it. Losing a message costs a display update, an accounting
row or an echo — never the completion, the liveness or the delivery of a task.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from new_core.identity import OwnerScope

MAX_BACKPRESSURE_WAIT_SEC = 300.0


@dataclass(frozen=True)
class AgentEvent:
    """Base for every message that crosses the transport.

    ``scope`` names whose work the message describes. It is required on the base and
    declared nowhere else: a kind added later cannot forget to say whose it is, and a
    transport can read identity off any message without naming a kind — a partition key is
    the one thing a broker needs and the one thing these interfaces must not learn a kind
    to obtain.

    ``droppable`` is on the kind rather than on the queue the message happened to land in.
    A bounded transport may discard a kind that says so; every other kind waits for room,
    because the message it is holding up is the user's answer and regenerates nowhere.
    """

    scope: OwnerScope

    droppable: ClassVar[bool] = False


@dataclass(frozen=True)
class OutboundText(AgentEvent):
    """Text bound for the owner's chat: the answer, a progress line, a proactive note.

    ``source`` is a routing key rather than an identity axis — the scope beside it says
    whose conversation this is — and ``transport`` carries the delivery hints a reply
    anchors to. Never droppable: this is the message the user is waiting for.
    """

    text: str = ""
    log_text: str | None = None
    fmt: str = ""
    is_progress: bool = False
    task_id: str = ""
    source: str = ""
    transport: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""
    ts: str = ""


@dataclass(frozen=True)
class TypingStarted(AgentEvent):
    """The chat's typing indicator, on. Droppable: the next one supersedes it."""

    droppable: ClassVar[bool] = True

    source: str = ""
    trace_id: str = ""
    ts: str = ""


@dataclass(frozen=True)
class TypingStopped(AgentEvent):
    """The chat's typing indicator, off. Droppable, and the transport times it out anyway."""

    droppable: ClassVar[bool] = True

    source: str = ""
    trace_id: str = ""
    ts: str = ""


@dataclass(frozen=True)
class TaskHeartbeat(AgentEvent):
    """A beat for the live logs. The lease itself was written by the running body."""

    droppable: ClassVar[bool] = True

    task_id: str = ""
    task_type: str = ""
    phase: str = ""
    trace_id: str = ""
    ts: str = ""


@dataclass(frozen=True)
class LiveLogLine(AgentEvent):
    """One session-only log line. Droppable: it has nowhere else to be read from."""

    droppable: ClassVar[bool] = True

    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LlmUsageReported(AgentEvent):
    """One model call's tokens and call metadata, billed to the scope that made it."""

    task_id: str = ""
    model: str = ""
    provider: str = ""
    api_key_type: str = ""
    model_category: str = ""
    category: str = "task"
    source: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    ts: str = ""


@dataclass(frozen=True)
class TaskMetrics(AgentEvent):
    """What one run cost in wall time and tool calls."""

    task_id: str = ""
    task_type: str = ""
    duration_sec: float = 0.0
    tool_calls: int = 0
    tool_errors: int = 0
    ts: str = ""


@dataclass(frozen=True)
class TaskFinished(AgentEvent):
    """An echo that a run ended, for the live card and the observability stream.

    The record is already terminal when this is published: the body wrote it. A consumer
    records the echo and performs no status transition.
    """

    task_id: str = ""
    task_type: str = ""
    status: str = ""
    result: str = ""
    total_rounds: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    trace_id: str = ""
    ts: str = ""


@dataclass(frozen=True)
class FileSendRequested(AgentEvent):
    """An attachment staged on disk, asked to be delivered to the owner's chat.

    ``file_path`` names a file under the outbound staging directory, which is where the
    consumer will look for it — the only place it accepts a path from.
    """

    task_id: str = ""
    file_path: str = ""
    filename: str = ""
    mime: str = ""
    caption: str = ""
    kind: str = "file"
    cleanup_after_send: bool = False
    source: str = ""
    transport: dict[str, Any] = field(default_factory=dict)
    ts: str = ""


@dataclass(frozen=True)
class PhotoSendRequested(AgentEvent):
    """An image carried inline as base64, staged by the consumer before delivery."""

    task_id: str = ""
    image_base64: str = ""
    mime: str = "image/png"
    caption: str = ""
    source: str = ""
    transport: dict[str, Any] = field(default_factory=dict)
    ts: str = ""


@dataclass(frozen=True)
class SubtaskRequested(AgentEvent):
    """A run asking for another task to be enqueued. Enqueuing is the supervisor's job."""

    task_id: str = ""
    description: str = ""
    context: str = ""
    depth: int = 0
    parent_task_id: str = ""
    root_task_id: str = ""
    source: str = ""
    transport: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""
    ts: str = ""
    suppress_ack: bool = False


@dataclass(frozen=True)
class TaskCancellationRequested(AgentEvent):
    """A run asking that one of its owner's tasks be cancelled."""

    task_id: str = ""
    job_id: str = ""
    source: str = ""
    transport: dict[str, Any] = field(default_factory=dict)
    ts: str = ""
    suppress_ack: bool = False


@dataclass(frozen=True)
class OwnerMessageInjected(AgentEvent):
    """An owner message was injected into a run mid-flight; logged for duplicate detection."""

    task_id: str = ""
    text: str = ""
    ts: str = ""


@dataclass(frozen=True)
class SkillLifecycle(AgentEvent):
    """A skill execution finished or failed, for the event stream and the in-process bus."""

    event_type: str = ""
    task_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = ""


class IAgentEventPublisher(ABC):
    """The producer half of the seam: what a run reports outward through."""

    @abstractmethod
    def publish(self, event: AgentEvent) -> None:
        """Hand one message to the transport.

        Blocks while a bounded transport is full, unless the kind declares itself
        droppable. A run that cannot report its answer must not race ahead of its output.
        """


class IAgentEventConsumer(ABC):
    """The consuming half of the seam: what reports are read from."""

    @abstractmethod
    def poll(self, timeout: float = 0.0) -> AgentEvent | None:
        """Return the next message, or ``None`` if none arrives within ``timeout``.

        The timeout is what saves a polling loop from needing its own sleep.
        """


class AgentEventBacklogFull(RuntimeError):
    pass


class NullAgentEventPublisher(IAgentEventPublisher):
    """Accepts every message and delivers none.

    What a run built outside a runtime holds — a test harness, a one-off agent — so a
    producer never has to ask whether anybody is listening.

    It still refuses a value no wire format carries. Delivering nothing is not the same as
    accepting anything: a producer whose free-form mapping holds a path would otherwise
    pass every test that runs against this publisher and fail on the first real transport,
    which is the exact failure the check exists to prevent.
    """

    def publish(self, event: AgentEvent) -> None:
        # Imported here because the codec imports this module for the kind set; the cycle
        # is real and the call is not on any hot path — this publisher delivers nothing.
        from ouroboros.agent_events.codec import ensure_wire_safe

        ensure_wire_safe(event)
        return None


__all__ = [
    "MAX_BACKPRESSURE_WAIT_SEC",
    "AgentEvent",
    "AgentEventBacklogFull",
    "FileSendRequested",
    "IAgentEventConsumer",
    "IAgentEventPublisher",
    "LiveLogLine",
    "LlmUsageReported",
    "NullAgentEventPublisher",
    "OutboundText",
    "OwnerMessageInjected",
    "PhotoSendRequested",
    "SkillLifecycle",
    "SubtaskRequested",
    "TaskCancellationRequested",
    "TaskFinished",
    "TaskHeartbeat",
    "TaskMetrics",
    "TypingStarted",
    "TypingStopped",
]
