"""A gateway for transport tests: assertable, with no supervisor behind it.

A transport under test needs two things the live bridge used to provide — a place
its submissions land where the test can read them, and a way for the test to act
as the supervisor and publish an answer back. Neither needs a running supervisor,
which is the point of the seam.

The outbound half is the real in-process outbox rather than a second
implementation of the same semantics, so a transport test cannot pass against a
fake that quietly disagrees with production about redelivery or stalling; it only
adds a record of what was published.
"""

from __future__ import annotations

import itertools
import queue
from collections.abc import Callable

from background_memory.interfaces import IMemoryEventPublisher, MemoryEvent
from tests.helpers.scope import scope_for
from transport.codec import ensure_wire_safe
from transport.infra.inmemory import InProcessOutbox
from transport.interfaces import (
    DeliveredMessage,
    IInbox,
    IMessageGateway,
    InboundEnvelope,
    IOutbox,
    IOutboxReader,
    OutboundMessage,
    SubscriptionMode,
)


class FakeInbox(IInbox):
    """Records what a transport submitted, and can be told to refuse.

    Type-checked like the real inbox: a transport that submits a dict or a
    pydantic model would otherwise pass its whole test file and raise on the
    first real message.
    """

    def __init__(self) -> None:
        self.submitted: list[InboundEnvelope] = []
        self.failure: Exception | None = None
        self._queue: queue.Queue[InboundEnvelope] = queue.Queue()

    def submit(self, envelope: InboundEnvelope) -> None:
        if not isinstance(envelope, InboundEnvelope):
            raise TypeError(f"inbox takes an InboundEnvelope, got {type(envelope).__name__}")
        ensure_wire_safe(envelope)
        if self.failure is not None:
            raise self.failure
        self.submitted.append(envelope)
        self._queue.put(envelope)

    def poll(self, timeout: float = 0.0) -> InboundEnvelope | None:
        """Read in order; ``submitted`` stays intact for assertions.

        A timeout of zero or less does not block, like the real inbox: the
        supervisor's drain polls with ``0.0`` after its first message.
        """
        try:
            return self._queue.get(timeout=timeout) if timeout > 0 else self._queue.get_nowait()
        except queue.Empty:
            return None

    def fail_with(self, error: Exception | None) -> None:
        """Make the next submissions raise, so a caller's retry can be tested."""
        self.failure = error


class SettlingReader(IOutboxReader):
    """A reader with no queue behind it, for testing one handler in isolation.

    A transport's outbound handler settles through the reader it was given, so a
    test that calls the handler directly needs one; ``deliver`` mints the
    delivery and ``settled`` records what the handler did with it.
    """

    def __init__(self) -> None:
        self.settled: list[str] = []
        self._receipts = itertools.count(1)

    def deliver(self, message: OutboundMessage) -> DeliveredMessage:
        """One delivery of ``message``, as ``poll`` would have returned it."""
        return DeliveredMessage(message, f"receipt-{next(self._receipts)}")

    @property
    def acked(self) -> int:
        return self.settled.count("ack")

    @property
    def nacked(self) -> int:
        return self.settled.count("nack")

    @property
    def deferred(self) -> int:
        return self.settled.count("defer")

    def poll(self, timeout: float = 0.0) -> DeliveredMessage | None:
        del timeout  # nothing produces here; the test hands messages to `deliver`
        return None

    def ack(self, delivered: DeliveredMessage) -> None:
        del delivered
        self.settled.append("ack")

    def nack(self, delivered: DeliveredMessage) -> None:
        del delivered
        self.settled.append("nack")

    def defer(self, delivered: DeliveredMessage, *, delay: float) -> None:
        del delivered, delay
        self.settled.append("defer")

    def close(self) -> None:
        return None


class RecordingOutbox(IOutbox):
    """The in-process outbox plus a record of everything published."""

    def __init__(self) -> None:
        self._outbox = InProcessOutbox()
        self.published: list[OutboundMessage] = []
        self.attempts: list[tuple[OutboundMessage, float | None]] = []
        self.failure: Exception | None = None

    def reader(self, *, name: str, mode: SubscriptionMode) -> IOutboxReader:
        return self._outbox.reader(name=name, mode=mode)

    def publish(self, message: OutboundMessage, *, before_publish: Callable[[], float] | None = None) -> None:
        self.attempts.append((message, before_publish() if before_publish is not None else None))
        if self.failure is not None:
            raise self.failure
        # Recorded only once the outbox has taken it, so a refused payload leaves
        # no trace here either.
        self._outbox.publish(message, before_publish=before_publish)
        self.published.append(message)


class FakeGateway(IMessageGateway):
    """What a transport is constructed with in a test."""

    def __init__(self) -> None:
        self._inbox = FakeInbox()
        self._outbox = RecordingOutbox()

    @property
    def inbox(self) -> FakeInbox:
        return self._inbox

    @property
    def outbox(self) -> RecordingOutbox:
        return self._outbox

    async def asubmit(self, envelope: InboundEnvelope) -> None:
        self._inbox.submit(envelope)

    async def apublish(self, message: OutboundMessage) -> None:
        self._outbox.publish(message)

    async def aclose(self) -> None:
        pass

    # -- what the test does as the supervisor -----------------------------

    def answer(self, chat_id: int | str, text: str, *, source: str, **kwargs) -> OutboundMessage:
        """Publish an answer as the supervisor would, and return it."""
        kwargs.setdefault("scope", scope_for(source, chat_id))
        message = OutboundMessage(source=source, text=text, **kwargs)
        self._outbox.publish(message)
        return message

    @property
    def submitted(self) -> list[InboundEnvelope]:
        return self._inbox.submitted

    @property
    def published(self) -> list[OutboundMessage]:
        return self._outbox.published


class RecordingMemoryEvents(IMemoryEventPublisher):
    """What a transport announced to background memory, without a runtime behind it."""

    def __init__(self) -> None:
        self.published: list[MemoryEvent] = []

    def publish(self, event: MemoryEvent) -> None:
        self.published.append(event)
