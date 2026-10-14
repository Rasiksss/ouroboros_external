"""Chat transports: the seam between user-facing transports and the supervisor.

Transports (dialogs, browser, a2a, skill bridge) import this package and nothing
else in the repo — the dependency runs the other way. A broker-backed gateway can
later replace the in-process queues (multi-instance) without any transport
changing, because the backend hides behind these ABCs.

Two rules govern everything below, and getting either wrong is silent:

* **The seam is synchronous.** ``submit``, ``publish``, ``poll`` and ``close``
  block. Browser and a2a run on asyncio, dialogs and the supervisor loop on
  threads; a synchronous seam is the one shape all of them can hold. An async
  transport runs its reader in a thread and hands results to its loop with
  ``call_soon_threadsafe``.
* **Filtering happens on the transport side, never in the queue.** A reader gets
  everything on its subscription and decides what is its business. A message that
  is not its business is ``reader.ack`` plus a drop — never ``nack``, which would
  hold the offset and stall everything behind it.

Interfaces are ABCs, not protocols: each seam has exactly one production
implementation (the gateway adapter in supervisor) and a small test fake, so the
shape is a contract to implement, not a property to duck-type. Messages are
frozen dataclasses, like in ``background_memory.interfaces``.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from new_core.identity import OwnerScope
from ouroboros.contracts.attachments import AttachmentMetadata

if TYPE_CHECKING:
    from ouroboros.storage.runtime_models import TaskRecord


def routed_chat_id(scope: OwnerScope) -> int | str:
    """The chat a scope addresses, in the type the transports route on.

    One rule for both halves of the seam: the chat a message is delivered to and the chat its
    row records cannot come from different conversions. What is single is the rule, not the
    type it yields — a reader that can only route numbers decides on its own side.
    """
    return routed_value(scope.chat_id)


def routed_value(text: str) -> int | str:
    """The rule: a canonical decimal spelling becomes its integer, everything else stays as it is."""
    try:
        value = int(text)
    except ValueError:
        return text
    return value if str(value) == text else text


class OutboundKind(str, Enum):
    """What an outbound message carries: content, media, or a chat action."""

    TEXT = "text"
    PHOTO = "photo"
    FILE = "file"
    ACTION = "action"


class SubscriptionMode(str, Enum):
    """Who is allowed to deliver a message — not "durable or not".

    ``SHARED`` is competing consumers: exactly one reader across all instances
    receives a given message, and progress survives a restart. It fits a
    transport that can deliver from any instance (dialogs sends over a plain API
    call), and the broker's partition assignment gives the per-chat claim for
    free.

    ``BROADCAST`` is one independent subscription per instance: every instance
    receives every message, and a reader begins at the present moment rather than
    replaying what it missed. It fits a transport whose delivery target is a
    connection held in this process (a browser WebSocket, a pending a2a request);
    an undelivered message belongs to a connection that died with the process.

    A ``BROADCAST`` subscription does not outlive its process, whether or not
    ``close`` ran, and it never degrades into competing consumers. Both follow
    from what the mode is for: a subscription that survives its process hands a
    restarted instance messages addressed to sockets that are gone, and a shared
    position hands a message to an instance holding no client for it, which acks
    and drops it on its own side — so the message reaches nobody.

    The implementation derives the consumer identity from the reader's name and,
    for ``BROADCAST``, the instance. The instance never appears in transport code.
    """

    SHARED = "shared"
    BROADCAST = "broadcast"


@dataclass(frozen=True)
class AttachmentRef:
    """A pointer to bytes held by the shared storage provider.

    Media travels by reference, never inline: base64 inflates by a third and a
    single photo exceeds a broker's default message ceiling, so an inline design
    works in-process and fails on the day it matters. A transport resolves the
    reference through the storage provider at delivery time.
    """

    attachment_id: str
    filename: str = ""
    mime: str = ""
    size: int = 0


@dataclass(frozen=True)
class InboundEnvelope:
    """A normalized user message a transport hands to the supervisor.

    The only inbound envelope in the system, and plain types only apart from the
    minted ``scope``: transports must not import the supervisor's pydantic models,
    and the supervisor takes this type directly rather than converting to one of
    its own.

    ``scope`` is the identity the transport builds once at ingress, from the instance's
    configured owner and agent plus its own ``TRANSPORT_NAME``, and
    carried unchanged to the agent's run state. ``chat_id`` and ``source`` read off
    it rather than being fields of their own, so nothing here can disagree with it.
    """

    scope: OwnerScope
    user_id: int | str | None = None
    text: str = ""
    sender_label: str = ""
    sender_session_id: str = ""
    client_message_id: str = ""
    correlation_id: str = ""
    trace_id: str = ""
    transport: dict[str, Any] = field(default_factory=dict)
    image_base64: str = ""
    image_mime: str = "image/jpeg"
    image_caption: str = ""
    attachment_ids: list[str] = field(default_factory=list)
    attachments: list[AttachmentMetadata] = field(default_factory=list)
    detached: bool = False
    suppress_chat_log: bool = False
    #: For a submission nobody typed: the acceptance acknowledgement has no reader. Refusals
    #: and failures are still answered, because those are news the owner does not otherwise get.
    suppress_acceptance_notice: bool = False
    #: A click on the question's card: only the confirmation route sets these, never typed text.
    confirmation_id: str = ""
    confirmation_decision: str = ""

    def __post_init__(self) -> None:
        if not str(self.client_message_id or "").strip():
            object.__setattr__(self, "client_message_id", uuid.uuid4().hex)

    @property
    def chat_id(self) -> str:
        """The chat this message belongs to, read from the minted scope."""
        return self.scope.chat_id

    @property
    def source(self) -> str:
        """The surface this message arrived on, read from the minted scope."""
        return self.scope.surface_id

    def effective_text(self) -> str:
        """The text a user actually said: body, else image caption, else a stub."""
        text = str(self.text or "").strip()
        if text:
            return text
        caption = str(self.image_caption or "").strip()
        if caption:
            return caption
        if self.image_base64:
            return "(image attached)"
        if self.attachment_ids:
            return "(file attached)"
        return ""


@dataclass(frozen=True)
class OutboundMessage:
    """One supervisor-produced message bound for a user: payload only.

    This is what ``IOutbox.publish`` takes and what crosses the wire, so it
    carries no acknowledgement of its own: settling a delivery belongs to
    ``IOutboxReader``, the side that holds a position to move. A payload that
    knew how to settle itself would stop being serializable the moment the
    backend is a broker.

    ``scope`` and ``source`` answer different questions and may legitimately differ.
    The scope is identity: whose work produced this, for which agent, on which surface,
    in which conversation — read off the producing work, never minted here. The source is
    routing: which transport carries it. It is required and rejected when empty, because
    delivery is decided by the reading transport, so a message with no source reaches
    nobody or everybody.

    There is no ``chat_id`` field beside the scope. Two fields carrying one fact will
    eventually name two different chats, and this one travels far from where it was
    checked; the chat a reader routes on is derived instead.
    """

    scope: OwnerScope
    source: str
    kind: OutboundKind = OutboundKind.TEXT
    text: str = ""
    action: str = ""
    caption: str = ""
    is_progress: bool = False
    markdown: bool = False
    task_id: str = ""
    correlation_id: str = ""
    trace_id: str = ""
    ts: str = ""
    transport: dict[str, Any] = field(default_factory=dict)
    attachment: AttachmentRef | None = None
    message_id: str = ""

    @property
    def chat_id(self) -> int | str:
        """The conversation a reader routes on, derived from the scope."""
        return routed_chat_id(self.scope)

    def __post_init__(self) -> None:
        if not str(self.source or "").strip():
            raise ValueError("outbound message requires an explicit source")


#: The ``OutboundMessage.source`` values some transport claims. Anything else is
#: acknowledged and dropped by every reader, so it reaches nobody. ``"web"`` is
#: absent on purpose: no transport has ever claimed it.
DELIVERY_SOURCES: frozenset[str] = frozenset({"browser", "dialogs", "sberchat", "a2a"})


@dataclass(frozen=True)
class DeliveredMessage:
    """What ``IOutboxReader.poll`` returns: a payload plus its receipt.

    The receipt names one delivery, not one message, and is what the reader is
    handed back to settle it. Identifying the delivery by payload would work
    in-process and fail on a broker, where the same payload can be redelivered
    and two equal payloads are indistinguishable.

    It has no default on purpose — a defaulted receipt would silently swallow a
    backend that forgot to issue a real one.
    """

    message: OutboundMessage
    receipt: str
    attempts: int = 0


class IInbox(ABC):
    """The inbound half: what transports submit and the supervisor loop polls.

    Exactly two methods, and no third one is coming: settling an inbound message
    is ``poll``'s own second job. The outbound half needs receipts because a
    subscription outlives its readers and two equal payloads must stay
    distinguishable; inbound holds at most one unsettled envelope per inbox, so a
    receipt would name something that was never ambiguous while adding a map to
    every backend and a second way for a consumer to be wrong — ack the wrong
    one, ack twice, or forget and stall forever.

    Inbound is therefore at-least-once, bounded to one message. The journal
    (``transport/journal.py``) covers the half before ``submit`` returns; this
    covers the half after it, which no sweep can reach: by then the transport has
    given up ownership and its row already says processed.
    """

    @abstractmethod
    def submit(self, envelope: InboundEnvelope) -> None:
        """Durably hand one message to the supervisor.

        Returns when the message has been accepted for delivery; raises when it
        has not, so the caller can leave its own record unprocessed and retry
        later. It answers nothing about what the message became — intent, task
        id, slash command — because that is decided elsewhere and, in a
        multi-instance deployment, on another machine.
        """

    @abstractmethod
    def poll(self, timeout: float = 0.0) -> InboundEnvelope | None:
        pass


class IOutboxReader(ABC):
    """One subscription over everything published for a user.

    The reader receives everything on its subscription: no method here takes a
    chat id, a source, a message kind or a progress flag as a selector, because a
    skipped-and-unacknowledged message holds its position and stalls the messages
    behind it.

    Settling lives here rather than on ``DeliveredMessage`` because the position
    it moves belongs to the subscription: the payload is the same object on every
    reader that receives it, and only this one knows what it has in flight.
    """

    @abstractmethod
    def poll(self, timeout: float = 0.0) -> DeliveredMessage | None:
        pass

    @abstractmethod
    def ack(self, delivered: DeliveredMessage) -> None:
        """Confirm this reader is finished with ``delivered``.

        Also the point at which resources staged for the message are released,
        such as inbound temporary files: an unacknowledged message must still
        find them on the retry.

        A receipt this reader no longer holds settles nothing, which is what
        makes a delivery that finishes after ``close`` harmless rather than a
        message taken from whoever holds it now.
        """

    @abstractmethod
    def nack(self, delivered: DeliveredMessage) -> None:
        """Report that an attempted delivery of ``delivered`` failed transiently.

        Only for a delivery that was attempted and failed. A message that is not
        this transport's business is ``ack`` plus a drop. A stale receipt is
        ignored the same way it is on ``ack``.
        """

    @abstractmethod
    def defer(self, delivered: DeliveredMessage, *, delay: float) -> None:
        """Retry later without spending an attempt; preserve owner order, free other owners."""

    @abstractmethod
    def close(self) -> None:
        """Release the reader: stop consuming, cancel the underlying subscription.

        Interrupts a concurrent ``poll`` rather than letting it run out its
        timeout.

        The orderly release, and not the only one: this is exactly the call a
        crashing process skips. The implementation releases a subscription whose
        process did not return without depending on that process to do it, and
        observes the calls a live reader already makes rather than gaining a
        liveness method here.
        """


class IOutbox(ABC):
    """The outbound half of the seam: everything bound for a user.

    Written by the supervisor (agent responses), the skill bridge (skills acting
    as the agent), and a transport for content that must reach connections other
    instances hold — the browser's echo of a user's own line. Read by every
    transport through its own reader.

    A transport is a producer only for content whose recipients are not
    necessarily its own process's connections. It publishes nothing it could
    deliver from the sockets it holds, because that would be a message every other
    reader has to receive and drop for no gain.
    """

    @abstractmethod
    def reader(self, *, name: str, mode: SubscriptionMode) -> IOutboxReader:
        pass

    @abstractmethod
    def publish(self, message: OutboundMessage, *, before_publish: Callable[[], float] | None = None) -> None:
        pass


class IMessageGateway(ABC):
    """The composition a transport receives: one seam for both directions.

    Inbox and outbox are separate interfaces on purpose: a transport writes to the
    inbox and reads from the outbox, and the split keeps the two directions
    independently testable and substitutable. The one exception is a transport
    publishing content whose recipients are held by other instances — see
    ``IOutbox`` — which is a producer role, not a second read path.
    """

    @property
    @abstractmethod
    def inbox(self) -> IInbox: ...

    @property
    @abstractmethod
    def outbox(self) -> IOutbox: ...

    @abstractmethod
    async def asubmit(self, envelope: InboundEnvelope) -> None: ...

    @abstractmethod
    async def apublish(self, message: OutboundMessage) -> None: ...

    @abstractmethod
    async def aclose(self) -> None: ...


class ITaskStopNotifier(ABC):
    """Notify runtimes about task stops already committed by the caller."""

    @abstractmethod
    def notify(self, records: Iterable[TaskRecord]) -> None: ...


class IEnvelopeBuilder(ABC):
    """Turns one stored journal row into this transport's inbound envelope."""

    @abstractmethod
    def build(self, record: Any) -> InboundEnvelope:
        pass


__all__ = [
    "DELIVERY_SOURCES",
    "AttachmentRef",
    "DeliveredMessage",
    "IEnvelopeBuilder",
    "IInbox",
    "IMessageGateway",
    "IOutbox",
    "IOutboxReader",
    "ITaskStopNotifier",
    "InboundEnvelope",
    "OutboundKind",
    "OutboundMessage",
    "SubscriptionMode",
    "routed_chat_id",
    "routed_value",
]
