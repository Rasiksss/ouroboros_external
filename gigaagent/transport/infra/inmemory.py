"""The single-instance backend behind the seam: in-process queues, no persistence.

Imported by the composition root alone — neither a transport nor the supervisor
names this module. The semantics are the ones a broker would give (competing
consumers for `SHARED`, per-instance subscriptions for `BROADCAST`, redelivery of
what was read but never acknowledged), so a broker-backed gateway can replace it
with nothing else changing; `tests/helpers/transport_conformance.py` is the
contract both must satisfy.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable

from transport.codec import ensure_wire_safe
from transport.infra.pending import PendingDelivery
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

log = logging.getLogger(__name__)

#: One permitted way to meet the seam's outbox-wide retention bound: here a
#: subscription's name *is* its consumer name, so bounding per subscription still
#: drops the oldest and still names the consumer that stopped consuming. A
#: backend holding one shared sequence bounds the sequence instead and names the
#: consumer whose position is furthest behind.
_MAX_PENDING_PER_SUBSCRIPTION = 10_000


class InProcessInbox(IInbox):
    """Everything transports submitted, waiting for the supervisor loop.

    Holds the half of the settlement contract it can: an envelope is returned
    once and never again, because taking it off the queue is what settles it.

    It cannot hold the other half. An unsettled envelope is meant to reach the
    consumer that replaces the one that stopped, and here the consumer that
    stopped takes the process, the queue and the envelope with it — there is
    nothing left to redeliver to. Redelivery binds a backend whose queue can span
    processes, which is where the guarantee is needed and where its conformance
    case runs.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[InboundEnvelope] = queue.Queue()

    def submit(self, envelope: InboundEnvelope) -> None:
        if not isinstance(envelope, InboundEnvelope):
            raise TypeError(f"inbox takes an InboundEnvelope, got {type(envelope).__name__}")
        # Checked here even though nothing is serialized in-process: without it a
        # producer passes every in-process test and fails on the broker.
        ensure_wire_safe(envelope)
        self._queue.put(envelope)

    def poll(self, timeout: float = 0.0) -> InboundEnvelope | None:
        # Taking the envelope off the queue *is* the settlement here, which is the
        # same fact as the deviation above: nothing is kept to redeliver. A slot
        # holding the last envelope would be state nothing in this backend can
        # read — the consumer that would receive it again is gone with the process.
        # A timeout of zero or less must not block: the drain polls with 0.0.
        try:
            return self._queue.get(timeout=timeout) if timeout > 0 else self._queue.get_nowait()
        except queue.Empty:
            return None


class _Subscription:
    """The queue state behind one or more readers.

    One per reader for ``BROADCAST``; one per name for ``SHARED``, which is what
    makes shared readers compete and lets one of them pick up what another
    closed without acknowledging.

    At most one message is in flight at a time, so an unacknowledged message
    holds its position and stalls the ones behind it — the property that makes
    queue-level filtering unworkable and drop-with-``ack`` mandatory.
    """

    def __init__(self, *, durable: bool, name: str) -> None:
        self.durable = durable
        self.name = name
        self._pending: deque[PendingDelivery] = deque()
        self.in_flight: PendingDelivery | None = None
        self.readers = 0
        self.closed = False
        self.dropped = 0
        #: For ``BROADCAST``: the one reader this subscription belongs to, held
        #: weakly so a reader dropped without ``close`` can be reaped. ``None``
        #: for ``SHARED``, which outlives its readers on purpose.
        self.owner: weakref.ref[_InProcessReader] | None = None

    @property
    def backlog(self) -> int:
        return len(self._pending)

    def offer(self, message: OutboundMessage) -> bool:
        """Append one message, dropping the oldest once the backlog is full.

        Returns whether the drop is due a report, which the caller makes *after*
        the fan-out. Logging here would put a call that can raise between two
        subscriptions, and a publish that raised halfway is the partial fan-out
        the seam forbids.
        """
        overflowed = False
        if len(self._pending) >= _MAX_PENDING_PER_SUBSCRIPTION:
            self._pending.popleft()
            overflowed = self._note_drop()
        self._pending.append(PendingDelivery(message))
        return overflowed

    def requeue(self, message: PendingDelivery) -> None:
        """Put an unacknowledged message back at the head of the backlog.

        Overflow drops the oldest, like ``offer``: requeueing happens under
        exactly the pressure that makes the retention policy matter, so it must
        not invert there.
        """
        if len(self._pending) >= _MAX_PENDING_PER_SUBSCRIPTION:
            self._pending.popleft()
            if self._note_drop():
                self.report_drop()
        self._pending.appendleft(message)

    def take(self) -> tuple[PendingDelivery | None, float]:
        if self.in_flight is not None or not self._pending:
            return None, float("inf")
        self.in_flight, delay = PendingDelivery.take(self._pending, time.monotonic())
        return self.in_flight, delay

    def discard_pending(self) -> None:
        """Drop a backlog the subscription it belongs to cannot outlive."""
        self._pending.clear()

    def _note_drop(self) -> bool:
        """Count one dropped message; True when this one is due a report."""
        self.dropped += 1
        return self.dropped == 1 or self.dropped % 1000 == 0

    def report_drop(self) -> None:
        """Name the consumer that stopped consuming; rate limited by ``_note_drop``."""
        log.error(
            "Outbox backlog for %r is full (%d); dropped %d message(s). Its transport is not consuming.",
            self.name,
            _MAX_PENDING_PER_SUBSCRIPTION,
            self.dropped,
        )


class _InProcessReader(IOutboxReader):
    def __init__(self, outbox: InProcessOutbox, subscription: _Subscription) -> None:
        self._outbox = outbox
        self._subscription = subscription
        self._closed = False
        #: The one delivery this reader can settle, by receipt. A broker would
        #: keep a map here; in flight is capped at one, so a pair is enough.
        self._in_flight: tuple[str, PendingDelivery] | None = None

    def poll(self, timeout: float = 0.0) -> DeliveredMessage | None:
        deadline = time.monotonic() + max(0.0, timeout)
        condition = self._outbox._condition
        with condition:
            while True:
                if self._closed or self._subscription.closed:
                    return None
                message, delay = self._subscription.take()
                if message is not None:
                    receipt = self._outbox._issue_receipt()
                    self._in_flight = (receipt, message)
                    return DeliveredMessage(message.message, receipt, message.attempts)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                condition.wait(min(remaining, delay))

    def ack(self, delivered: DeliveredMessage) -> None:
        self._settle(delivered, requeue=False, delay=0)

    def nack(self, delivered: DeliveredMessage) -> None:
        self._settle(delivered, requeue=True, delay=0)

    def defer(self, delivered: DeliveredMessage, *, delay: float) -> None:
        self._settle(delivered, requeue=True, delay=max(0.001, delay))

    def close(self) -> None:
        """Release the reader, waking any concurrent ``poll``.

        A message this reader never acknowledged goes back to the head of a
        durable subscription for a competing reader to take; an ephemeral
        subscription drops it, because it belonged to a connection that died
        with this reader. The last reader takes the whole subscription with it.

        Callers stop their reader loop and join it before closing: a message
        still being delivered when the reader is released is requeued, and the
        delivery that then succeeds cannot take it back.
        """
        condition = self._outbox._condition
        with condition:
            if self._closed:
                return
            self._closed = True
            # Dropping the receipt is what makes a delivery that finishes after
            # this point settle nothing: the message has gone back to the head of
            # the queue and belongs to whichever reader takes it next.
            in_flight = self._in_flight
            self._in_flight = None
            self._outbox._release_reader(self._subscription, in_flight[1] if in_flight else None)
            condition.notify_all()

    def _settle(self, delivered: DeliveredMessage, *, requeue: bool, delay: float) -> None:
        """Finish with ``delivered``, if its receipt is the one this reader holds.

        Two guards, because a shared subscription outlives its readers and only
        the reader still holding a delivery may clear or requeue the in-flight
        slot: the receipt must be the one this reader was issued, and the
        subscription must still hold that same message. A call that arrives late
        — after the message went back to the queue and another reader took it —
        fails one of them and does nothing.
        """
        condition = self._outbox._condition
        with condition:
            in_flight = self._in_flight
            if in_flight is None or in_flight[0] != delivered.receipt:
                return
            message = in_flight[1]
            self._in_flight = None
            subscription = self._subscription
            if subscription.in_flight is not message:
                return
            subscription.in_flight = None
            if requeue:
                message.available_at = time.monotonic() + delay
                message.attempts += int(delay == 0)
                subscription.requeue(message)
            condition.notify_all()


class InProcessOutbox(IOutbox):
    """Everything published for a user, read by every transport.

    Published once, offered to every subscription; who delivers it is decided on
    the transport side. No method here narrows a reader's stream.

    ``publish`` is atomic by construction rather than by luck: the fan-out runs
    inside the one condition every reader also polls under, so there is no point
    at which some subscriptions hold the message and others do not, and the only
    way out of the call without offering it to all of them is a raise before the
    loop starts.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._shared: dict[str, _Subscription] = {}
        self._broadcast: list[_Subscription] = []
        # The name-to-mode claim. A dict in this process satisfies the
        # cross-instance rule only because this backend *is* one process: a
        # backend whose subscriptions span processes must hold the claim where
        # every instance can read it, or a rolling deploy opens one name in two
        # modes and every message is delivered twice.
        self._modes: dict[str, SubscriptionMode] = {}
        self._receipts = itertools.count(1)

    def reader(self, *, name: str, mode: SubscriptionMode) -> IOutboxReader:
        transport_name = str(name or "").strip()
        if not transport_name:
            raise ValueError("an outbox reader requires the transport's name")
        with self._condition:
            # The name is the consumer identity, and a consumer has one mode: two
            # modes under one name are two subscriptions the publisher fans into,
            # which is a duplicate delivery on a broker as much as here.
            claimed = self._modes.setdefault(transport_name, mode)
            if claimed is not mode:
                raise ValueError(
                    f"subscription {transport_name!r} is already open as {claimed.value}, not {mode.value}"
                )
            if mode is SubscriptionMode.SHARED:
                subscription = self._shared.get(transport_name)
                if subscription is None:
                    subscription = _Subscription(durable=True, name=transport_name)
                    self._shared[transport_name] = subscription
                subscription.readers += 1
                return _InProcessReader(self, subscription)
            # Broadcast starts at the present: an undelivered message belongs to
            # a connection this instance no longer holds.
            subscription = _Subscription(durable=False, name=transport_name)
            subscription.readers += 1
            reader = _InProcessReader(self, subscription)
            # Weakly, and before the subscription is registered, so that every
            # subscription in `_broadcast` has an owner by construction — see
            # `_reap_abandoned_broadcast`.
            subscription.owner = weakref.ref(reader)
            self._broadcast.append(subscription)
            return reader

    def publish(self, message: OutboundMessage, *, before_publish: Callable[[], float] | None = None) -> None:
        if not isinstance(message, OutboundMessage):
            raise TypeError(f"outbox takes an OutboundMessage, got {type(message).__name__}")
        ensure_wire_safe(message)
        with self._condition:
            if before_publish is not None:
                before_publish()
            self._reap_abandoned_broadcast()
            self._offer_to_all(message)
            self._condition.notify_all()

    def _offer_to_all(self, message: OutboundMessage) -> None:
        """The append: every open subscription takes it; ``_condition`` held.

        Named rather than inlined because it is the step the seam's atomicity
        rule is about — everything open at the call takes the message, or the
        call leaves without any of them having it.

        Every append runs before anything that can raise: a drop is *reported*
        after the fan-out rather than during it, so there is no subscription that
        can take the message while a later one does not.
        """
        overflowed = [
            subscription for subscription in (*self._shared.values(), *self._broadcast) if subscription.offer(message)
        ]
        for subscription in overflowed:
            subscription.report_drop()

    def _issue_receipt(self) -> str:
        """A tag for one delivery, unique across this outbox; ``_condition`` held.

        Counted per outbox rather than per reader: two readers on one shared
        subscription must never mint the same receipt, or a stale settle would
        land on the wrong one.
        """
        return str(next(self._receipts))

    def _release_reader(self, subscription: _Subscription, in_flight: PendingDelivery | None) -> None:
        """Called with ``_condition`` held when a reader closes.

        ``in_flight`` is the delivery the closing reader still holds, and only
        that one may be taken back: the subscription's slot belongs to whichever
        reader took it, and a shared subscription outlives its readers.
        """
        subscription.readers = max(0, subscription.readers - 1)
        if in_flight is not None and subscription.in_flight is in_flight:
            subscription.in_flight = None
            if subscription.durable:
                subscription.requeue(in_flight)
            # A non-durable subscription keeps nothing back: its undelivered
            # message was addressed to a connection that died with this reader,
            # and a later process must start owed nothing.
        if subscription.readers > 0:
            return
        subscription.closed = True
        if subscription.durable:
            # This backend's declared deviation from the `SHARED` durability
            # rule, and the only class of backend allowed to take it: in one
            # process there is no second consumer, so a subscription with no
            # reader is a write-only sink and a 10 000-message blast at the next
            # open is worse than a drop that says so. A backend whose
            # subscriptions can span processes must keep the backlog instead —
            # there the reader that died has peers that can finish its work.
            self._shared.pop(subscription.name, None)
            if subscription.backlog:
                log.warning(
                    "Dropping the %r subscription with its last reader; %d pending message(s) go with it.",
                    subscription.name,
                    subscription.backlog,
                )
        elif subscription in self._broadcast:
            self._broadcast.remove(subscription)
        subscription.discard_pending()

    def _reap_abandoned_broadcast(self) -> None:
        """Release broadcast subscriptions whose reader is gone; ``_condition`` held.

        ``close`` is the orderly release and cannot be the only one. A reader
        dropped without it is the one-process form of an instance that did not
        come back: nothing can ever poll that subscription again, and every
        publish would keep feeding it until retention was its only bound.
        """
        for subscription in [item for item in self._broadcast if item.owner is not None and item.owner() is None]:
            subscription.closed = True
            subscription.readers = 0
            self._broadcast.remove(subscription)
            if subscription.backlog:
                log.warning(
                    "Releasing the %r subscription whose reader is gone; %d pending message(s) go with it.",
                    subscription.name,
                    subscription.backlog,
                )
            subscription.discard_pending()


class InProcessMessageGateway(IMessageGateway):
    """The composition a transport receives: one seam for both directions."""

    async def asubmit(self, envelope: InboundEnvelope) -> None:
        self.inbox.submit(envelope)

    async def apublish(self, message: OutboundMessage) -> None:
        self.outbox.publish(message)

    async def aclose(self) -> None:
        pass

    def __init__(self, *, inbox: InProcessInbox, outbox: InProcessOutbox) -> None:
        self._inbox = inbox
        self._outbox = outbox

    @property
    def inbox(self) -> InProcessInbox:
        return self._inbox

    @property
    def outbox(self) -> InProcessOutbox:
        return self._outbox


__all__ = [
    "InProcessInbox",
    "InProcessMessageGateway",
    "InProcessOutbox",
]
