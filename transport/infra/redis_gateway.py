"""The multi-instance backend behind the seam: Redis Streams, one stream per direction.

Imported by composition roots alone — neither a transport nor the supervisor names this
module, exactly as with its in-process sibling `transport/infra/inmemory.py`, and
`tests/helpers/transport_conformance.py` is the contract both satisfy. What it adds over
`transport/codec.py` is one thing and one only: a JSON envelope in a single stream-entry
field. The codec still owns the wire format, so a payload arrives as the types it declares
rather than as an equal collection of JSON values.

Four facts explain most of the code below:

* **A consumer group does not stall on an unacknowledged entry.** The seam requires that it
  does, so the reader gates itself: while it holds an unsettled delivery it issues no Redis
  command at all.
* **`SHARED` is a group per name and `BROADCAST` a group per open reader**, so the set of
  groups on the stream *is* the name-to-mode claim every instance reads. There is no second
  record of it.
* **A `BROADCAST` subscription must not outlive its process even when `close` never ran.**
  The reader refreshes a heartbeat key on the calls it already makes, and `publish` sweeps
  the groups whose key is gone — the seam forbids adding a liveness method to it.
* **A broker that is not there is never raised out of `RedisInbox.poll`.** That poll is the
  pacing call of the supervisor's tick; the policy is
  `ouroboros/contracts/stream_replies.py::BrokerOutage`, shared
  with the other two stream readers. `submit` still raises, because a producer that could
  not hand its message over must hear so.

The client is expected to decode replies to `str`; every read is normalised anyway, because
a client built without `decode_responses` would otherwise fail as wrong values rather than
as an error. The normalisation itself — the reply shapes, the id arithmetic, the blocking
budget — is `ouroboros/contracts/stream_replies.py`, shared with the agent-event backend so
the two readers cannot drift.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack
from copy import copy
from typing import Any

import redis
import redis.asyncio
from redis.backoff import NoBackoff
from redis.retry import Retry
from redis.sentinel import MasterNotFoundError, SentinelConnectionPool

from infra.redis_client import BROKER_SOCKET_TIMEOUT_SEC
from ouroboros.contracts.stream_replies import (
    BROKER_REPORT_INTERVAL_SEC,
    BROKER_RETRY_SEC,
    BrokerOutage,
    block_ms,
    entry_key as _entry_key,
    first_entry as _first_entry,
    is_existing_group,
    is_missing_group as _is_missing_group,
    row as _row,
    text as _text,
)
from transport.codec import (
    decode_inbound,
    decode_outbound,
    encode_inbound,
    encode_outbound,
    ensure_wire_safe,
)
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

#: The one field of a stream entry: the codec's mapping, JSON-encoded.
_PAYLOAD_FIELD = "payload"

#: Separates a subscription name from the mode marker and the instance that opened it. A
#: reader name may not contain it, so a name cannot forge a group shape.
_SEPARATOR = "#"
_BROADCAST_MARKER = f"{_SEPARATOR}b{_SEPARATOR}"
#: The consumer a closing `SHARED` reader puts its unsettled entry on. It never polls, so a
#: zero min-idle claim from it touches only entries somebody deliberately put down.
_RELEASED_CONSUMER_SUFFIX = f"{_SEPARATOR}released"

#: The other permitted way to meet the seam's outbox-wide retention bound (see
#: `transport/infra/inmemory.py`): one shared sequence, so the bound is on the sequence and
#: the report names the consumer group whose position is furthest behind.
_MAX_STREAM_ENTRIES = 10_000
_OUTBOX_SCAN_BATCH = 32

#: How long a broadcast group survives without its reader refreshing the heartbeat key.
#: 120x the production poll timeout of 0.5 s (`transport/browser/ws.py`), and the cost of
#: getting it wrong is loud rather than silent: the next read fails with NOGROUP and
#: `transport/reader_loop.py` stops the transport's consumption.
_BROADCAST_HEARTBEAT_TTL_SEC = 60

#: A pass of the sweep and the crash backstop costs round trips where the in-process sibling
#: walks a list, so each is gated to at most one pass per interval.
_MAINTENANCE_INTERVAL_SEC = 5.0

#: Derived from the TTL rather than shared with the interval above: a refresh rate somebody
#: raised to save round trips would otherwise let the sweep destroy the group of a *live*
#: reader, and nothing would connect the two numbers.
_HEARTBEAT_REFRESH_SEC = _BROADCAST_HEARTBEAT_TTL_SEC / 4

#: How long an *outbound* entry must sit unsettled before another consumer may take it. It
#: must exceed the longest single delivery the `SHARED` transports perform, because only they
#: can have two readers on one group: that is dialogs, whose one outbound call is bounded at
#: `_DIALOGS_OUTBOUND_TIMEOUT_SEC` (120 s, `transport/dialogs/outbound.py`) and which holds
#: the in-flight slot across it. The browser's much smaller `_MESSAGE_BUDGET_SEC` is the wrong
#: measure here — the browser is `BROADCAST`, so its group has one consumer and no peer to
#: reclaim from. Below the right bound a slow live reader's message reaches the user twice.
_OUTBOX_RECLAIM_MIN_IDLE_MS = 300_000

#: The inbound equivalent, and much larger, because the window it must exceed is a different
#: one: an inbound entry stays unsettled from the poll that returned it until this consumer's
#: *next* poll, which for the supervisor is a whole tick — a drain of up to 100 envelopes plus
#: everything else the tick does. Below that bound a peer reclaims an envelope a live
#: instance already turned into a task, and the user's message runs twice. Oversized, the only
#: cost is that a crashed instance's one unsettled envelope waits longer.
_INBOX_RECLAIM_MIN_IDLE_MS = 300_000

#: The drop report is a report of a *state*, not of an event: the condition persists until
#: somebody drains the group, so a pass of the maintenance gate would repeat it every few
#: seconds forever. This is the seam's "rate limited".
_DROP_REPORT_INTERVAL_SEC = 300.0

#: A blocking read is sliced so `close` can interrupt it: redis-py connections cannot safely
#: be closed from another thread while a blocking command is in flight.
_POLL_SLICE_MS = 250

#: How many unreadable envelopes one inbox poll may drop before it returns to the caller.
#: The skip has to exist — an entry nobody can decode would otherwise be read forever — but
#: it must be bounded: the skip is a `continue` past the deadline check, and an entry that
#: arrives from *this consumer's pending list* comes back on every iteration, so without a
#: cap a single undecodable inherited envelope is a poll that never returns and a supervisor
#: tick that spins at a live readiness probe. Its sibling on the memory seam has the same
#: number and the same reason (`background_memory/infra/events/redis_transport.py`).
MAX_SKIPPED_ENTRIES_PER_POLL = 32


class _Throttle:
    """At most one pass per interval; the interval is read at the call site each time."""

    def __init__(self) -> None:
        self._next_pass = 0.0

    def due(self, interval_sec: float) -> bool:
        now = time.monotonic()
        if now < self._next_pass:
            return False
        self._next_pass = now + interval_sec
        return True


def _payload_of(fields: Mapping[Any, Any]) -> str:
    return str(_row(fields).get(_PAYLOAD_FIELD, ""))


def _first_claimed(response: Any) -> tuple[str, Mapping[Any, Any]] | None:
    """The first `(id, fields)` of an `XCLAIM` reply."""
    for entry_id, fields in response or ():
        if fields is None:
            continue
        return (str(_text(entry_id)), fields)
    return None


def _slice_ms(deadline: float) -> int:
    """Milliseconds to block for on the next read; zero means do not block."""
    return block_ms(deadline, _POLL_SLICE_MS)


def _ensure_group(client: redis.Redis, stream: str, group: str, *, start: str) -> None:
    """Create the group, treating `BUSYGROUP` as "it exists, resume from where it is"."""
    try:
        client.xgroup_create(stream, group, id=start, mkstream=True)
    except redis.exceptions.ResponseError as error:
        if not is_existing_group(error):
            raise


class RedisInbox(IInbox):
    """Everything transports submitted, waiting for the supervisor loop.

    `IInbox` has two methods and no third is coming, so settling is `poll`'s own second job:
    the entry returned last is `XACK`ed at the top of the next call. That costs two round
    trips per envelope — the supervisor's drain (`supervisor/runtime.py`, limit 100) pays
    the `XACK` on every `poll(0.0)` too, because skipping it would settle nothing — and it
    buys the redelivery the seam asks for: an entry taken and never settled stays in this
    consumer's pending list, where the instance that replaces this one finds it.

    The consumer name is required and has no default. Two supervisor instances sharing one
    would share a pending list, and each would read the other's in-flight entry as its own
    redelivery.
    """

    def __init__(
        self, *, client: redis.Redis, append_client: redis.Redis, stream: str, group: str, consumer: str
    ) -> None:
        if not str(stream or "").strip() or not str(group or "").strip():
            raise ValueError("a redis inbox requires its stream and group")
        if not str(consumer or "").strip():
            raise ValueError("a redis inbox requires this instance's own consumer name")
        self._client = client
        #: The append has its own client because redis-py's retry is per connection, not per
        #: command: reads must be retried on a dropped connection and this one must not.
        self._append_client = append_client
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._group_ready = False
        #: The entry the previous `poll` returned, settled at the top of the next one.
        self._unsettled = ""
        #: Read this consumer's pending list before `>` until it comes back empty once: a
        #: replacement presenting the same name finds its predecessor's envelope with no
        #: idle delay, and after that the list holds at most the entry `poll` just settled.
        self._drain_pending = True
        self._reclaim = _Throttle()
        self._trim = _Throttle()
        #: The policy the three stream readers share: an I/O failure in `poll` is `None` and
        #: a paced wait, never a raise. `submit` is deliberately not covered — a producer
        #: that could not hand its message over must hear so, and its journal row is what
        #: replays it.
        self._outage = BrokerOutage(
            logger=log,
            retry_after_sec=BROKER_RETRY_SEC,
            report_every_sec=BROKER_REPORT_INTERVAL_SEC,
        )

    @staticmethod
    def _payload(envelope: InboundEnvelope) -> str:
        if not isinstance(envelope, InboundEnvelope):
            raise TypeError(f"inbox takes an InboundEnvelope, got {type(envelope).__name__}")
        ensure_wire_safe(envelope)
        return json.dumps(encode_inbound(envelope))

    def submit(self, envelope: InboundEnvelope) -> None:
        payload = self._payload(envelope)
        self._ready()
        self._append_client.xadd(self._stream, {_PAYLOAD_FIELD: payload})

    async def asubmit(self, client: redis.asyncio.Redis, envelope: InboundEnvelope) -> None:
        payload = self._payload(envelope)
        if not self._group_ready:
            try:
                await client.xgroup_create(self._stream, self._group, id="0", mkstream=True)
            except redis.exceptions.ResponseError as error:
                if not is_existing_group(error):
                    raise
            self._group_ready = True
        await client.xadd(self._stream, {_PAYLOAD_FIELD: payload})

    def poll(self, timeout: float = 0.0) -> InboundEnvelope | None:
        """The next envelope, or ``None`` once ``timeout`` has passed. Never raises on I/O.

        This poll is the pacing call of the supervisor's tick (``supervisor/runtime.py``).
        A broker failure is recorded, the caller's remaining timeout is spent waiting, and the polls that follow
        issue no command until the retry interval has passed. Nothing is lost by waiting:
        an unsettled envelope stays in this consumer's pending list.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        if self._outage.stands(deadline):
            return None
        try:
            self._settle_previous()
            self._ready()
            self._drop_settled()
            envelope = self._drain(deadline)
        except redis.exceptions.RedisError as error:
            if _is_missing_group(error):
                # A group that vanished is not a broker that is gone: the recovery is to
                # recreate it here rather than to sit out the retry interval, and every call
                # that owns the recovery itself has already made it by now (`_read`,
                # `_settle_previous`) — this catches the ones that do not, `_decoded`'s
                # acknowledgement above all.
                self._regroup()
                return None
            self._group_ready = False
            self._outage.record(
                deadline,
                "The inbound stream %r cannot be read (%s); retrying every %.0fs. The tick keeps "
                "running and the envelopes already submitted are drained when it answers again.",
                self._stream,
                error,
                BROKER_RETRY_SEC,
            )
            return None
        self._outage.cleared("The inbound stream %r can be read again", self._stream)
        return envelope

    def _drain(self, deadline: float) -> InboundEnvelope | None:
        skipped = 0
        while True:
            entry = self._pending() or self._reclaimed() or _first_entry(self._read(">", _slice_ms(deadline)))
            if entry is not None:
                decoded = self._decoded(entry)
                if decoded is None:
                    skipped += 1
                    if skipped >= MAX_SKIPPED_ENTRIES_PER_POLL:
                        # Back to the caller rather than draining the whole backlog here:
                        # this `continue` is past the deadline check, and an entry from the
                        # pending list is returned again on every iteration.
                        return None
                    continue
                self._unsettled = entry[0]
                return decoded
            if time.monotonic() >= deadline:
                return None

    def _ready(self) -> None:
        if self._group_ready:
            return
        # From `0`, not `$`: an envelope submitted before the supervisor tier ever polled is
        # exactly what the durability of this half is for.
        _ensure_group(self._client, self._stream, self._group, start="0")
        self._group_ready = True

    def _regroup(self) -> None:
        """Recreate a group that is gone, so `NOGROUP` is a hiccup rather than a dead end.

        A Redis restart takes every group with it (this backend runs without persistence, and
        says so). Caching "the group exists" for the life of the object turned that into an
        inbox that raises on every call for the rest of the process: the supervisor's tick
        crashes, and the envelopes still in the stream reach nobody. Recreated at `0` like the
        first time, so entries the vanished group never delivered are delivered now; ones it
        had already handled arrive twice, which the inbound path collapses on the task id
        derived from `client_message_id`.
        """
        self._group_ready = False
        self._drain_pending = True
        self._ready()

    def _settle_previous(self) -> None:
        if not self._unsettled:
            return
        # Cleared first: a failed XACK leaves the entry in the pending list, which is the
        # redelivery the seam asks for, rather than an id this inbox retries forever.
        entry_id, self._unsettled = self._unsettled, ""
        try:
            self._client.xack(self._stream, self._group, entry_id)
        except redis.exceptions.ResponseError as error:
            if not _is_missing_group(error):
                raise
            self._regroup()

    def _drop_settled(self) -> None:
        """Remove the entries every group has finished with; throttled.

        `XACK` clears an entry from a pending list and leaves it in the stream forever, so
        without this a *healthy* deployment — one draining normally — grows `{prefix}:inbound`
        without limit until Redis's own `maxmemory` policy decides: `noeviction` fails every
        `submit` and stops ingress, anything else evicts the whole key, undelivered envelopes
        and consumer groups together.

        The floor is the earliest entry any group could still deliver: its oldest pending
        entry, or the one after its last-delivered id when it holds none. Trimming below that
        removes only what every group has settled, which is why this is a bound on *garbage*
        rather than the retention bound the outbox has — a backlog nobody has read is never
        touched, and `submit`'s promise survives.
        """
        if not self._trim.due(_MAINTENANCE_INTERVAL_SEC):
            return
        try:
            floor = self._settled_floor()
            if floor is not None:
                self._client.xtrim(self._stream, minid=floor)
        except Exception:
            # Housekeeping never fails a poll: the entries stay and the next pass retries.
            log.warning("Could not trim the settled entries of %r", self._stream, exc_info=True)

    def _settled_floor(self) -> str | None:
        """The earliest id any group might still deliver, or `None` when nothing may go."""
        groups = [_row(group) for group in self._client.xinfo_groups(self._stream)]
        if not groups:
            return None  # nothing has ever consumed this stream, so nothing is settled
        floors = []
        for group in groups:
            name = str(_text(group.get("name")))
            summary = self._client.xpending(self._stream, name)
            oldest = _row(summary).get("min") if isinstance(summary, Mapping) else None
            if oldest:
                floors.append(_entry_key(oldest))
                continue
            delivered = _entry_key(group.get("last-delivered-id") or "0-0")
            floors.append((delivered[0], delivered[1] + 1))
        milliseconds, sequence = min(floors)
        return f"{milliseconds}-{sequence}"

    def _pending(self) -> tuple[str, Mapping[Any, Any]] | None:
        if not self._drain_pending:
            return None
        entry = _first_entry(self._read("0", 0))
        if entry is None:
            self._drain_pending = False
        return entry

    def _reclaimed(self) -> tuple[str, Mapping[Any, Any]] | None:
        """The backstop for a consumer name that never came back."""
        if not self._reclaim.due(_MAINTENANCE_INTERVAL_SEC):
            return None
        try:
            claimed = self._client.xautoclaim(
                self._stream, self._group, self._consumer, _INBOX_RECLAIM_MIN_IDLE_MS, start_id="0-0", count=1
            )
        except redis.exceptions.ResponseError as error:
            if _is_missing_group(error):
                return None
            raise
        return _first_claimed(claimed[1] if len(claimed) > 1 else ())

    def _read(self, last_id: str, block_ms: int) -> Any:
        try:
            return self._client.xreadgroup(
                self._group, self._consumer, {self._stream: last_id}, count=1, block=block_ms or None
            )
        except redis.exceptions.ResponseError as error:
            if not _is_missing_group(error):
                raise
            self._regroup()
            return None

    def _decoded(self, entry: tuple[str, Mapping[Any, Any]]) -> InboundEnvelope | None:
        """The envelope, or `None` after acknowledging and dropping one that cannot decode.

        Redelivering an entry nothing can read stalls the subscription forever, and the
        seam's own rule for a message a reader cannot use is `ack` plus a drop.

        The `XACK` is what makes the drop final, on both paths that reach here. An entry
        delivered by `>` and left unacknowledged sits in this consumer name's pending list
        for good, where the instance that replaces this one — the redelivery `_drain_pending`
        exists for — reads it before anything else, on every iteration, past the deadline
        check; the same holds for an entry `_regroup` put back. Without it the poll spends
        its whole skip budget on one unreadable envelope, every tick, forever.
        """
        entry_id, fields = entry
        try:
            return decode_inbound(json.loads(_payload_of(fields)))
        except Exception:
            log.error("Dropping inbound entry %s of %r: it does not decode", entry_id, self._stream, exc_info=True)
            self._client.xack(self._stream, self._group, entry_id)
            return None


class _RedisOutboxReader(IOutboxReader):
    """One subscription over everything published for a user.

    It holds the client it was opened with rather than reading the outbox's, so a producer
    whose connection broke does not break a reader that is still consuming.
    """

    def __init__(
        self,
        *,
        client: redis.Redis,
        stream: str,
        group: str,
        consumer: str,
        released_consumer: str,
        mode: SubscriptionMode,
        heartbeat_key: str,
    ) -> None:
        self._client = client
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._released_consumer = released_consumer
        self._mode = mode
        self._heartbeat_key = heartbeat_key
        self._waiting_key = f"{stream}:waiting:{group}"
        self._retry_key = f"{stream}:retry:{group}"
        self._attempts_key = f"{stream}:attempts:{group}"
        self._pending_cursor = "0"
        self._reclaim_cursor = "0-0"
        self._cleanup_cursor = 0
        self._cleanup = _Throttle()
        self._condition = threading.Condition()
        self._closed = False
        #: The one delivery this reader can settle: its receipt and the entry it names.
        self._in_flight: tuple[str, str] | None = None
        self._receipts = itertools.count(1)
        self._reclaim = _Throttle()
        self._heartbeat = _Throttle()
        #: The policy the readers on both halves of this seam share. Without it a dropped
        #: connection is a raise, and a raise here is terminal: it reaches
        #: `transport/reader_loop.py`, which stops consuming and hands back to a caller that
        #: closes the reader and never reopens it.
        self._outage = BrokerOutage(
            logger=log,
            retry_after_sec=BROKER_RETRY_SEC,
            report_every_sec=BROKER_REPORT_INTERVAL_SEC,
        )

    def poll(self, timeout: float = 0.0) -> DeliveredMessage | None:
        """The next delivery, or ``None`` once ``timeout`` has passed. Never raises on I/O.

        The policy `RedisInbox.poll` states, for a worse consequence on this side: a broker
        that is not there stops this instance delivering for the rest of its life while its
        readiness probe still answers 200, because a new connection re-enters
        `attach_gateway` with the same gateway and is turned away. The group `_regroup`
        recreates covers `NOGROUP`; this covers the broker.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        if self._outage.stands(deadline):
            return None
        try:
            delivered = self._deliver_next(deadline)
        except redis.exceptions.RedisError as error:
            self._outage.record(
                deadline,
                "The outbound subscription %r cannot be read (%s); retrying every %.0fs. The "
                "transport keeps consuming and takes what is still in the stream when it "
                "answers again.",
                self._group,
                error,
                BROKER_RETRY_SEC,
            )
            return None
        self._outage.cleared("The outbound subscription %r can be read again", self._group)
        return delivered

    def _deliver_next(self, deadline: float) -> DeliveredMessage | None:
        # Before anything else, including the stall gate below: a broadcast reader that
        # receives nothing for a whole TTL is still live, and letting the sweep take its
        # group would stop it receiving at all.
        self._touch_heartbeat()
        self._prune_waiting()
        while True:
            with self._condition:
                if self._closed:
                    return None
                if self._in_flight is not None:
                    # The stall a consumer group does not give: while this reader holds an
                    # unsettled delivery it issues no Redis command, and `ack`, `nack` and
                    # `close` are what wake it.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._condition.wait(remaining)
                    continue
            for entry in self._candidates(deadline):
                delivered = self._delivery(entry)
                if delivered is not None:
                    return delivered
            if time.monotonic() >= deadline:
                return None

    def ack(self, delivered: DeliveredMessage) -> None:
        self._settle(delivered, acknowledge=True, delay=0)

    def nack(self, delivered: DeliveredMessage) -> None:
        self._settle(delivered, acknowledge=False, delay=0)

    def defer(self, delivered: DeliveredMessage, *, delay: float) -> None:
        self._settle(delivered, acknowledge=False, delay=max(0.001, delay))

    def close(self) -> None:
        """Release the reader, waking a concurrent poll.

        A `SHARED` reader empties its pending list onto the reserved released consumer, where
        a competing reader takes what was there inside its next poll rather than after an idle
        bound, and then removes itself as a consumer. A `BROADCAST` reader takes its whole
        group with it: its undelivered messages belonged to connections that died with this
        process.
        """
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._in_flight = None
            self._condition.notify_all()
        if self._mode is SubscriptionMode.BROADCAST:
            self._destroy_group()
            return
        # The consumer itself is left behind. `XGROUP DELCONSUMER` would tidy it, and it is
        # refused: it destroys pending entries outright rather than leaving them claimable,
        # so any entry this release did not move — one the read below truncated, or one a
        # reader thread takes in the instant after it — would be lost with no backstop able
        # to see it. A `SHARED` group therefore keeps one consumer per reader ever opened;
        # reaping them safely needs an idle sweep with its own empty-pending check.
        self._release_pending()

    def _candidates(self, deadline: float) -> Iterator[tuple[str, Mapping[str, str]]]:
        response = self._read(self._pending_cursor, 0)
        pending = response[0][1] if response else []
        self._pending_cursor = _text(pending[-1][0]) if len(pending) == _OUTBOX_SCAN_BATCH else "0"
        for entry_id, fields in pending:
            if fields:
                yield _text(entry_id), fields
        for _ in range(_OUTBOX_SCAN_BATCH):
            entry = self._released()
            if entry is None:
                break
            yield entry
        entry = self._reclaimed()
        if entry is not None:
            yield entry
        for _ in range(_OUTBOX_SCAN_BATCH):
            response = self._read(">", _slice_ms(deadline))
            if not response:
                break
            for entry_id, fields in response[0][1]:
                if fields:
                    yield _text(entry_id), fields

    def _delivery(self, entry: tuple[str, Mapping[Any, Any]]) -> DeliveredMessage | None:
        message = self._decoded(entry)
        if message is None:
            return None
        with self._condition:
            if not self._closed:
                if not self._ready(entry[0], message):
                    return None
                attempts = int(self._client.hget(self._attempts_key, entry[0]) or 0)
                receipt = f"{self._group}:{self._consumer}:{entry[0]}:{next(self._receipts)}"
                self._in_flight = (receipt, entry[0])
                return DeliveredMessage(message, receipt, attempts)
        self._release_pending()
        return None

    def _settle(self, delivered: DeliveredMessage, *, acknowledge: bool, delay: float) -> None:
        with self._condition:
            in_flight = self._in_flight
            if in_flight is None or in_flight[0] != delivered.receipt:
                return
            entry_id = in_flight[1]
            member = self._waiting_member(entry_id, delivered.message)
            for _ in range(_OUTBOX_SCAN_BATCH):
                with self._client.pipeline() as pipeline:
                    try:
                        pipeline.watch(self._waiting_key)
                        retained = pipeline.zscore(self._waiting_key, member) is not None
                        seconds, micros = self._client.time()
                        pipeline.multi()
                        if acknowledge or not retained:
                            pipeline.xack(self._stream, self._group, entry_id)
                            pipeline.zrem(self._waiting_key, member)
                            pipeline.hdel(self._retry_key, entry_id)
                            pipeline.hdel(self._attempts_key, entry_id)
                        else:
                            pipeline.hset(self._retry_key, entry_id, seconds + micros / 1_000_000 + delay)
                            pipeline.hset(self._attempts_key, entry_id, delivered.attempts + int(delay == 0))
                        self._expire_state(pipeline)
                        pipeline.execute()
                        break
                    except redis.exceptions.WatchError:
                        continue
            else:
                raise redis.exceptions.WatchError("outbox settlement could not complete")
            self._in_flight = None
            self._condition.notify_all()
        self._touch_heartbeat()

    @staticmethod
    def _waiting_member(entry_id: str, message: OutboundMessage) -> str:
        owner = json.dumps([message.scope.owner_id, message.scope.agent_id]).encode().hex()
        milliseconds, sequence = _entry_key(entry_id)
        return f"{owner}:{milliseconds:020d}-{sequence:020d}"

    def _ready(self, entry_id: str, message: OutboundMessage) -> bool:
        member = self._waiting_member(entry_id, message)
        owner = member.split(":", 1)[0] + ":"
        if not self._enroll(member):
            return False
        for _ in range(_OUTBOX_SCAN_BATCH):
            first = self._client.zrangebylex(self._waiting_key, f"[{owner}", f"[{owner}~", start=0, num=1)
            if not first:
                return True
            head = _text(first[0])
            head_id = self._waiting_id(head)
            if not self._client.xrange(self._stream, head_id, head_id, count=1):
                with self._client.pipeline() as pipeline:
                    pipeline.zrem(self._waiting_key, head)
                    pipeline.hdel(self._retry_key, head_id)
                    pipeline.hdel(self._attempts_key, head_id)
                    pipeline.execute()
                continue
            if head != member:
                return False
            retry_at = float(self._client.hget(self._retry_key, entry_id) or 0)
            seconds, micros = self._client.time()
            return retry_at <= seconds + micros / 1_000_000
        return False

    def _enroll(self, member: str) -> bool:
        for _ in range(_OUTBOX_SCAN_BATCH):
            with self._client.pipeline() as pipeline:
                try:
                    pipeline.watch(self._waiting_key)
                    if pipeline.zscore(self._waiting_key, member) is not None:
                        return True
                    if pipeline.zcard(self._waiting_key) >= _MAX_STREAM_ENTRIES:
                        pipeline.unwatch()
                        self._prune_waiting(force=True)
                        return False
                    pipeline.multi()
                    pipeline.zadd(self._waiting_key, {member: 0})
                    self._expire_state(pipeline)
                    pipeline.execute()
                    return True
                except redis.exceptions.WatchError:
                    continue
        return False

    @staticmethod
    def _waiting_id(member: str) -> str:
        ms, seq = member.rsplit(":", 1)[1].split("-")
        return f"{int(ms)}-{int(seq)}"

    def _prune_waiting(self, *, force: bool = False) -> None:
        if not force and not self._cleanup.due(_MAINTENANCE_INTERVAL_SEC):
            return
        self._cleanup_cursor, rows = self._client.zscan(
            self._waiting_key, cursor=self._cleanup_cursor, count=_OUTBOX_SCAN_BATCH
        )
        members = [_text(row[0]) for row in rows]
        with self._client.pipeline() as pipeline:
            for member in members:
                entry_id = self._waiting_id(member)
                pipeline.xrange(self._stream, entry_id, entry_id, count=1)
            retained = pipeline.execute()
            for member, content in zip(members, retained, strict=True):
                if not content:
                    entry_id = self._waiting_id(member)
                    pipeline.zrem(self._waiting_key, member)
                    pipeline.hdel(self._retry_key, entry_id)
                    pipeline.hdel(self._attempts_key, entry_id)
                    pipeline.xack(self._stream, self._group, entry_id)
            pipeline.execute()

    def _released(self) -> tuple[str, Mapping[Any, Any]] | None:
        """What a `SHARED` reader put down when it closed.

        The consumer filter is what makes this safe with no idle bound at all: the released
        consumer never polls, so its pending list holds only entries a closing reader
        deliberately put down, never a live reader's in-flight entry.
        """
        if self._mode is not SubscriptionMode.SHARED:
            return None
        try:
            pending = self._client.xpending_range(
                self._stream, self._group, min="-", max="+", count=1, consumername=self._released_consumer
            )
            if not pending:
                return None
            entry_id = _text(_row(pending[0]).get("message_id"))
            return _first_claimed(self._client.xclaim(self._stream, self._group, self._consumer, 0, [entry_id]))
        except redis.exceptions.ResponseError as error:
            if _is_missing_group(error):
                return None  # `_read` owns the recovery; this call simply found nothing
            raise

    def _reclaimed(self) -> tuple[str, Mapping[Any, Any]] | None:
        """The backstop for an instance that never called `close`, on the throttled gate."""
        if not self._reclaim.due(_MAINTENANCE_INTERVAL_SEC):
            return None
        try:
            claimed = self._client.xautoclaim(
                self._stream,
                self._group,
                self._consumer,
                _OUTBOX_RECLAIM_MIN_IDLE_MS,
                start_id=self._reclaim_cursor,
                count=1,
            )
        except redis.exceptions.ResponseError as error:
            if _is_missing_group(error):
                return None
            raise
        self._reclaim_cursor = _text(claimed[0])
        return _first_claimed(claimed[1] if len(claimed) > 1 else ())

    def _read(self, last_id: str, block_ms: int) -> Any:
        try:
            if last_id == ">":
                return self._read_new(block_ms)
            return self._client.xreadgroup(
                self._group, self._consumer, {self._stream: last_id}, count=_OUTBOX_SCAN_BATCH, block=block_ms or None
            )
        except redis.exceptions.ResponseError as error:
            if not _is_missing_group(error):
                raise
            # `close` destroys a broadcast group, which unblocks this read with NOGROUP.
            if self._closed:
                return None
            self._regroup()
            return None

    def _read_new(self, block_ms: int) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        with self._condition:
            if self._closed:
                return []
            for _ in range(_OUTBOX_SCAN_BATCH):
                with self._client.pipeline() as pipeline:
                    try:
                        pipeline.watch(self._stream, self._waiting_key, self._retry_key)
                        groups = pipeline.xinfo_groups(self._stream)
                        group = next((_row(row) for row in groups if _text(_row(row).get("name")) == self._group), None)
                        if group is None:
                            raise redis.exceptions.ResponseError("NOGROUP")
                        cursor = _text(group.get("last-delivered-id"))
                        entries = pipeline.xrange(self._stream, min=f"({cursor}", max="+", count=1)
                        if not entries:
                            pipeline.unwatch()
                            with self._condition:
                                if not self._closed and block_ms:
                                    self._condition.wait(block_ms / 1000)
                            return []
                        if pipeline.zcard(self._waiting_key) >= _MAX_STREAM_ENTRIES:
                            pipeline.unwatch()
                            self._prune_waiting(force=True)
                            return []
                        members: dict[str, int] = {}
                        for entry_id, fields in entries:
                            if fields is None:
                                continue
                            try:
                                message = decode_outbound(json.loads(_payload_of(fields)))
                            except (TypeError, ValueError, KeyError):
                                continue
                            members[self._waiting_member(_text(entry_id), message)] = 0
                        pipeline.multi()
                        # Enrollment and stream position must move together, including across competing readers.
                        pipeline.xreadgroup(self._group, self._consumer, {self._stream: ">"}, count=1)
                        if members:
                            pipeline.zadd(self._waiting_key, members)
                        self._expire_state(pipeline)
                        return pipeline.execute()[0]
                    except redis.exceptions.WatchError:
                        continue
            return []

    def _regroup(self) -> None:
        """Recreate a group that vanished under a *live* reader, and say so.

        Three ways it goes: a Redis restart (this backend runs without persistence and says
        so), a sweep that found this reader's heartbeat missing because a `SET` failed —
        `_touch_heartbeat` swallows that by design, so a poll is never failed by liveness
        bookkeeping — or a reader whose thread was stalled past the TTL. Before this, any of
        them was terminal: the raise propagates through `transport/reader_loop.py`, which
        stops consuming and hands back to a caller that closes the reader and never reopens
        it, so the instance delivers nothing for the rest of its life while its readiness
        probe still answers 200. Recreated at `$` in both modes, exactly as `reader()` does:
        what was published while the group did not exist belonged to that group.
        """
        log.warning("The %r subscription vanished; recreating it and resuming at the present", self._group)
        _ensure_group(self._client, self._stream, self._group, start="$")
        # Ahead of the gate, or the sweep that took it takes it again before the next refresh.
        self._force_heartbeat()

    def _decoded(self, entry: tuple[str, Mapping[Any, Any]]) -> OutboundMessage | None:
        entry_id, fields = entry
        try:
            return decode_outbound(json.loads(_payload_of(fields)))
        except Exception:
            log.error("Dropping outbound entry %s of %r: it does not decode", entry_id, self._stream, exc_info=True)
            self._client.xack(self._stream, self._group, entry_id)
            return None

    def _release_pending(self) -> None:
        """Hand what this consumer still holds to the released pool, for a peer to take now.

        Read from the pending list rather than from the tracked delivery, for two reasons the
        tracked one gets wrong. A `nack` leaves its entry in the list with no in-flight
        receipt beside it, so the tracked delivery is `None` while an entry is still owed to
        somebody. And the crash backstop may have handed the tracked entry to a peer while
        this reader was slow — claiming *that* back would put a message another reader is
        delivering right now into the pool a third one takes it from, the guard
        `transport/infra/inmemory.py:346` states as "only the delivery this reader still
        holds". What the list holds is by definition neither.

        Nothing here has to be exhaustive, which is why the count is small and a failure only
        logs: what stays in this consumer's pending list stays claimable, and the idle
        backstop is what collects it. This is the fast path, not the safety net.
        """
        if self._mode is not SubscriptionMode.SHARED:
            return
        try:
            pending = self._client.xpending_range(
                self._stream, self._group, min="-", max="+", count=8, consumername=self._consumer
            )
            held: list[Any] = [str(_text(_row(item).get("message_id"))) for item in pending]
            if held:
                self._client.xclaim(self._stream, self._group, self._released_consumer, 0, held)
        except redis.exceptions.RedisError:
            # Transports close in a `finally`; the crash backstop covers the entries anyway.
            log.warning("Could not release what %r still holds on %r", self._consumer, self._stream, exc_info=True)

    def _destroy_group(self) -> None:
        try:
            with self._client.pipeline() as pipeline:
                pipeline.xgroup_destroy(self._stream, self._group)
                pipeline.delete(self._heartbeat_key, self._waiting_key, self._retry_key, self._attempts_key)
                pipeline.execute()
        except redis.exceptions.RedisError:
            log.warning("Could not destroy the broadcast group %r; the sweep will", self._group, exc_info=True)

    def _expire_state(self, pipeline: redis.client.Pipeline) -> None:
        if self._mode is SubscriptionMode.BROADCAST:
            for key in (self._waiting_key, self._retry_key, self._attempts_key):
                pipeline.expire(key, _BROADCAST_HEARTBEAT_TTL_SEC)

    def _touch_heartbeat(self) -> None:
        """Refresh the liveness a broadcast group is swept without.

        On `poll`, `ack` and `nack` — the calls a live reader already makes, because the
        seam forbids a liveness method of its own. On a gate of its own, derived from the TTL
        it renews rather than shared with the other housekeeping, so a poll every 0.5 s does
        not cost a round trip every 0.5 s.
        """
        if self._heartbeat.due(_HEARTBEAT_REFRESH_SEC):
            self._force_heartbeat()

    def _force_heartbeat(self) -> None:
        """Refresh regardless of the gate; a failure is logged rather than failing a poll."""
        if not self._heartbeat_key:
            return
        try:
            with self._client.pipeline() as pipeline:
                pipeline.set(self._heartbeat_key, "1", ex=_BROADCAST_HEARTBEAT_TTL_SEC)
                self._expire_state(pipeline)
                pipeline.execute()
        except redis.exceptions.RedisError:
            # Swallowed on purpose — liveness bookkeeping may not fail a delivery — and the
            # consequence is covered: a group swept because of this is recreated by `_regroup`.
            log.warning("Could not refresh the heartbeat of %r", self._group, exc_info=True)


class RedisOutbox(IOutbox):
    """Everything published for a user, read by every transport, on one stream.

    `publish` is a single `XADD` and the fan-out is by consumer-group position, so there is
    no state in which some subscriptions hold the message and others do not — the seam's
    all-or-nothing rule holds structurally rather than by discipline.
    """

    def __init__(self, *, client: redis.Redis, append_client: redis.Redis, stream: str) -> None:
        if not str(stream or "").strip():
            raise ValueError("a redis outbox requires its stream")
        self._client = client
        #: The append has its own client because redis-py's retry is per connection, not per
        #: command: reads must be retried on a dropped connection and this one must not.
        self._append_client = append_client
        self._stream = stream
        # Minted rather than derived, for the reason runtime ids are: a pid recycles and a
        # hostname is shared by two replicas on one node. `mint_runtime_id` is deliberately
        # not imported — the browser transport root may not reach `ouroboros.agent_runtime`.
        self._instance = uuid.uuid4().hex
        self._opens = itertools.count()
        self._maintenance = _Throttle()
        self._drop_report = _Throttle()

    def reader(self, *, name: str, mode: SubscriptionMode) -> IOutboxReader:
        transport_name = str(name or "").strip()
        if not transport_name:
            raise ValueError("an outbox reader requires the transport's name")
        if _SEPARATOR in transport_name:
            raise ValueError(f"a reader name may not contain {_SEPARATOR!r}: it separates a group's parts")

        # The claim is only current once orphaned groups are gone, and this is the read of
        # it: a broadcast claim lapses when the last group holding it is swept.
        self._sweep_orphaned_broadcast()
        claimed = self._claimed_mode(transport_name)
        if claimed is not None and claimed is not mode:
            raise ValueError(f"subscription {transport_name!r} is already open as {claimed.value}, not {mode.value}")

        consumer = f"{self._instance}{_SEPARATOR}{next(self._opens)}"
        if mode is SubscriptionMode.SHARED:
            group, heartbeat_key = transport_name, ""
        else:
            # A group per open reader, not per instance: two broadcast readers of one name
            # in one process must both receive the message rather than compete.
            group = f"{transport_name}{_BROADCAST_MARKER}{consumer}"
            heartbeat_key = self._heartbeat_key(group)
            # Set before the group exists, so a peer's sweep cannot take a group that is
            # being opened for one that was abandoned.
            self._client.set(heartbeat_key, "1", ex=_BROADCAST_HEARTBEAT_TTL_SEC)
        # At `$` in both modes: the in-process sibling fans out only to subscriptions that
        # exist at publish time, so replaying the stream at first open would be a behaviour
        # no other backend has.
        _ensure_group(self._client, self._stream, group, start="$")
        return _RedisOutboxReader(
            client=self._client,
            stream=self._stream,
            group=group,
            consumer=consumer,
            released_consumer=f"{transport_name}{_RELEASED_CONSUMER_SUFFIX}",
            mode=mode,
            heartbeat_key=heartbeat_key,
        )

    @staticmethod
    def _payload(message: OutboundMessage) -> str:
        if not isinstance(message, OutboundMessage):
            raise TypeError(f"outbox takes an OutboundMessage, got {type(message).__name__}")
        ensure_wire_safe(message)
        return json.dumps(encode_outbound(message))

    async def apublish(self, client: redis.asyncio.Redis, message: OutboundMessage) -> None:
        payload = self._payload(message)
        deadline = time.monotonic() + BROKER_SOCKET_TIMEOUT_SEC

        def execute(command: str, *args: str) -> object:
            if time.monotonic() >= deadline:
                raise TimeoutError("outbox maintenance deadline expired")
            return self._append_client.execute_command(command, *args)

        try:
            async with asyncio.timeout(BROKER_SOCKET_TIMEOUT_SEC):
                await asyncio.to_thread(self._maintain, execute)
        except TimeoutError:
            log.warning("Outbox maintenance on %r timed out", self._stream)
        finally:
            deadline = time.monotonic()
        await client.xadd(self._stream, {_PAYLOAD_FIELD: payload}, maxlen=_MAX_STREAM_ENTRIES, approximate=False)

    def publish(self, message: OutboundMessage, *, before_publish: Callable[[], float] | None = None) -> None:
        payload = self._payload(message)
        guard = before_publish
        if guard is None:
            self._append_client.xadd(
                self._stream, {_PAYLOAD_FIELD: payload}, maxlen=_MAX_STREAM_ENTRIES, approximate=False
            )
        else:
            # A call-owned pool makes connect/socket budgets local to this publication.
            # Keep the configured connection class (TCP, TLS, Unix or Sentinel).
            source = self._append_client.connection_pool
            remaining = guard()
            options = dict(source.connection_kwargs)
            options.update(retry=Retry(NoBackoff(), 0), retry_on_error=[], retry_on_timeout=False)
            for key in ("socket_connect_timeout", "socket_timeout"):
                options[key] = min(options.get(key) or remaining, remaining)
            with ExitStack() as resources:
                if isinstance(source, SentinelConnectionPool):
                    # Discover through owned clients: the source Sentinel proxy retains its own retries/timeouts.
                    manager = copy(source.sentinel_manager)
                    for sentinel in source.sentinel_manager.sentinels:
                        remaining = guard()
                        discovery = dict(sentinel.connection_pool.connection_kwargs)
                        discovery.update(retry=Retry(NoBackoff(), 0), retry_on_error=[], retry_on_timeout=False)
                        for key in ("socket_connect_timeout", "socket_timeout"):
                            discovery[key] = min(discovery.get(key) or remaining, remaining)
                        client = resources.enter_context(
                            redis.Redis.from_pool(
                                redis.ConnectionPool(
                                    connection_class=sentinel.connection_pool.connection_class, **discovery
                                )
                            )
                        )
                        manager.sentinels = [client]
                        try:
                            address = manager.discover_master(source.service_name)
                            break
                        except MasterNotFoundError:
                            guard()
                    else:
                        raise MasterNotFoundError(f"No master found for {source.service_name!r}")
                    # The owned proxy reuses this discovery; connection readiness still checks authority below.
                    manager.discover_master = lambda service_name: address
                    remaining = guard()
                    for key in ("socket_connect_timeout", "socket_timeout"):
                        options[key] = min(options[key], remaining)
                    options.pop("connection_pool")
                    pool: redis.ConnectionPool = SentinelConnectionPool(
                        source.service_name,
                        manager,
                        connection_class=source.connection_class,
                        check_connection=source.check_connection,
                        **options,
                    )
                else:
                    pool = redis.ConnectionPool(connection_class=source.connection_class, **options)
                connection = None
                try:
                    connection = pool.get_connection()
                    connection.check_health()

                    def execute(command: str, *args: Any) -> Any:
                        remaining = guard()  # Readiness or maintenance can outlive authority.
                        if remaining <= 0:
                            raise TimeoutError("outbox publication deadline expired")
                        connection.socket_timeout = min(options["socket_timeout"], remaining)
                        socket = getattr(connection, "_sock", None)
                        if socket is None:
                            raise ConnectionError("outbox connection is not ready")
                        socket.settimeout(connection.socket_timeout)
                        connection.send_command(command, *args, check_health=False)
                        return self._append_client.parse_response(connection, command)

                    # Observe earlier appends on this lifecycle-owned pass, before the final authority check.
                    # Nothing may delay returning an accepted XADD to its durable bookkeeping.
                    self._maintain(execute)
                    execute(
                        "XADD",
                        self._stream,
                        "MAXLEN",
                        "=",
                        _MAX_STREAM_ENTRIES,
                        "*",
                        _PAYLOAD_FIELD,
                        payload,
                    )
                finally:
                    if connection is not None:
                        pool.release(connection)
                    pool.disconnect()
        if guard is None:
            self._maintain()

    def _maintain(self, execute: Callable[..., Any] | None = None) -> None:
        if not self._maintenance.due(_MAINTENANCE_INTERVAL_SEC):
            return
        # One guard each, not one around both: they are independent, and sharing a guard let
        # a sweep that always fails — an ACL without `XGROUP`, a read-only replica — suppress
        # the drop report on every pass, so the only signal an operator has for a consumer
        # that stopped consuming was a warning that did not name it.
        #
        # Every exception, not only the client's: the promise above is that this cannot fail
        # the append, and an unexpected reply shape would break it as surely as a dropped
        # connection. An unrelated producer's turn is not failed by housekeeping.
        for step, task in (("drop report", self._report_dropped), ("sweep", self._sweep_orphaned_broadcast)):
            try:
                task(execute)
            except Exception:
                log.warning("Outbox %s on %r failed", step, self._stream, exc_info=True)

    def _sweep_orphaned_broadcast(self, execute: Callable[..., Any] | None = None) -> None:
        """Destroy broadcast groups whose reader stopped refreshing its heartbeat.

        `close` is the orderly release and cannot be the only one: a process that did not
        return leaves a subscription nothing will ever poll, which every publish would keep
        feeding until retention was its only bound.
        """
        execute = execute or self._client.execute_command
        for group in self._group_names(execute):
            if _BROADCAST_MARKER not in group or execute("EXISTS", self._heartbeat_key(group)):
                continue
            execute("XGROUP DESTROY", self._stream, group)
            execute("DEL", *(f"{self._stream}:{kind}:{group}" for kind in ("waiting", "retry", "attempts")))
            log.warning("Released the broadcast subscription %r: its reader is gone", group)

    def _report_dropped(self, execute: Callable[..., Any] | None = None) -> None:
        execute = execute or self._client.execute_command
        try:
            info = _row(execute("XINFO STREAM", self._stream))
            groups = execute("XINFO GROUPS", self._stream)
        except redis.exceptions.ResponseError as error:
            if _is_missing_group(error):
                return
            raise
        length = int(info.get("length") or 0)
        added = info.get("entries-added")
        if added is None:
            # `entries-added` arrived in Redis 7.0, which is this backend's minimum. Treating
            # its absence as "nothing was trimmed" turned an unsupported server into a
            # deployment with no drop reporting at all and nothing saying so.
            if self._drop_report.due(_DROP_REPORT_INTERVAL_SEC):
                log.error(
                    "Cannot tell whether %r has dropped messages: the server reports no "
                    "'entries-added', so it is below the Redis 7.0 this backend requires.",
                    self._stream,
                )
            return
        if int(added) <= length:
            return  # nothing was ever trimmed, so nobody is behind because of the bound
        first = info.get("first-entry")
        first_key = _entry_key(first[0]) if first else None
        behind = [
            row
            for row in (_row(group) for group in groups)
            if first_key is None or _entry_key(row.get("last-delivered-id") or "0-0") < first_key
        ]
        if not behind or not self._drop_report.due(_DROP_REPORT_INTERVAL_SEC):
            return
        # Largest lag first, then the earliest position: Redis reports no lag at all for a
        # group whose entries were trimmed, which is exactly this case.
        worst = min(behind, key=lambda row: (-int(row.get("lag") or 0), _entry_key(row.get("last-delivered-id") or 0)))
        log.error(
            "Outbox %r is at its retention bound (%d) and %r has lost messages. Its transport is not consuming.",
            self._stream,
            _MAX_STREAM_ENTRIES,
            _text(worst.get("name")),
        )

    def _claimed_mode(self, name: str) -> SubscriptionMode | None:
        """The mode this name is claimed in, read off the set of groups on the stream.

        Not a second record beside them: a `{prefix}:modes` hash would be a second source of
        truth for a fact the group list already carries, and nothing would reconcile the two.
        """
        groups = self._group_names()
        if name in groups:
            return SubscriptionMode.SHARED
        if any(group.startswith(f"{name}{_BROADCAST_MARKER}") for group in groups):
            return SubscriptionMode.BROADCAST
        return None

    def _group_names(self, execute: Callable[..., Any] | None = None) -> list[str]:
        try:
            groups = (execute or self._client.execute_command)("XINFO GROUPS", self._stream)
        except redis.exceptions.ResponseError as error:
            if _is_missing_group(error):
                return []
            raise
        return [str(_text(_row(group).get("name"))) for group in groups]

    def _heartbeat_key(self, group: str) -> str:
        return f"{self._stream}:hb:{group}"


class RedisMessageGateway(IMessageGateway):
    """The composition a transport receives: one seam for both directions."""

    def __init__(self, *, inbox: RedisInbox, outbox: RedisOutbox, async_client: redis.asyncio.Redis) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._async_client = async_client

    async def asubmit(self, envelope: InboundEnvelope) -> None:
        await self._inbox.asubmit(self._async_client, envelope)

    async def apublish(self, message: OutboundMessage) -> None:
        await self._outbox.apublish(self._async_client, message)

    async def aclose(self) -> None:
        await self._async_client.aclose()

    @property
    def inbox(self) -> RedisInbox:
        return self._inbox

    @property
    def outbox(self) -> RedisOutbox:
        return self._outbox


__all__ = [
    "RedisInbox",
    "RedisMessageGateway",
    "RedisOutbox",
]
