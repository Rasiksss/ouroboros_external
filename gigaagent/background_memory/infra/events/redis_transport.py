from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

import redis
import redis.asyncio

from background_memory.infra.events.codec import (
    UnknownMemoryEventKind,
    decode_memory_event,
    encode_memory_event,
)
from background_memory.infra.events.outage import AsyncBrokerOutage, wait_out
from background_memory.interfaces import (
    IMemoryEventConsumer,
    IMemoryEventPublisher,
    MemoryEvent,
)
from ouroboros.contracts.stream_replies import (
    BROKER_REPORT_INTERVAL_SEC,
    BROKER_RETRY_SEC,
    block_ms,
    first_entry,
    is_existing_group,
    is_missing_group,
    row,
)

log = logging.getLogger(__name__)

#: The stream entry's one field: the codec's mapping as JSON.
PAYLOAD_FIELD = "payload"

#: How many events the stream holds before the trim starts evicting the oldest. Its own
#: number rather than the in-process ``MAX_PENDING_EVENTS = 32``, because it answers a
#: different question: 32 is sized against one handler run occupying a loop that is about to
#: drain the queue, while this bounds what a Redis running without persistence holds for a
#: background-memory tier that is down or stalled. Reusing 32 would lose a task's
#: distillation to a two-second consumer stall.
#:
#: **Sized in bytes, expressed in entries**, because ``MAXLEN`` counts entries and the
#: payload here has no bound of its own: a ``MemoryDistillationEvent`` carries the whole
#: response text and the whole LLM trace, so tens of kilobytes each is ordinary and hundreds
#: are possible. At 2 000 that is tens of megabytes for a tier that has been down for hours,
#: in the same Redis that holds the gateway's inbound and outbound streams and the
#: agent-event stream — where reaching ``maxmemory`` would start failing *their* appends,
#: including the owner's outgoing message. A payload bound of its own belongs to the
#: producer and is recorded as deferred debt rather than imposed here by truncation.
REDIS_MAX_STREAM_ENTRIES = 2_000

#: The longest a single blocking read asks Redis to wait. Below the socket timeout the
#: composition root configures (`ouroboros/app/broker_composition.py`), or redis-py raises on
#: a blocking read that is doing exactly what it was asked to do. A longer caller timeout is
#: served by repeating the read until the caller's own deadline.
MAX_POLL_BLOCK_SEC = 5.0

#: How many unreadable entries one poll may drop before it returns to the loop. The skip has
#: to exist — an entry nobody can decode would otherwise be re-read forever — but it must be
#: bounded: without a limit, one poll owns the memory loop for as long as a backlog of bad
#: entries takes to drain, and the loop's stop signal is only re-checked between polls
#: (`background_memory/runtime.py`).
MAX_SKIPPED_ENTRIES_PER_POLL = 32

#: The shared outage numbers, named here because this module's own text quotes them. The
#: policy they parameterise is this package's own `AsyncBrokerOutage`, which makes the same
#: decision the other two stream readers make and leaves the waiting to the caller — the
#: numbers stay shared so a broker that is not there means the same thing on all three seams.
_UNAVAILABLE_RETRY_SEC = BROKER_RETRY_SEC
_UNAVAILABLE_REPORT_INTERVAL_SEC = BROKER_REPORT_INTERVAL_SEC

#: How often the pending list is settled. A round trip where nothing else would need one, so
#: it is gated rather than run per poll.
_MAINTENANCE_INTERVAL_SEC = 5.0

#: How long an entry may sit in the pending list before the settle pass acks it. Reached only
#: by an entry whose ``XACK`` failed — it was decoded and handed to a handler already — so
#: the wait only has to be long enough that a settle can never race a live read.
_PENDING_IDLE_TTL_MS = 600_000

#: How many pending entries one settle pass may claim. Bounded for the reason every drain
#: here is: a pass that walked the whole list would own the memory loop.
_SETTLE_BATCH = 64


class RedisMemoryEventPublisher(IMemoryEventPublisher):
    """What a producer process reports completed work through.

    Takes the stream and the cap as arguments and reads no setting: a transport that read
    one could disagree with the root that built it
    (`openspec/specs/background-memory-events/spec.md`).
    """

    def __init__(self, *, client: redis.Redis, stream: str, max_entries: int) -> None:
        if not str(stream or "").strip():
            raise ValueError("a redis memory-event publisher requires its stream")
        if int(max_entries) <= 0:
            raise ValueError("a redis memory-event publisher requires a positive stream capacity")
        self._client = client
        self._stream = stream
        self._max_entries = int(max_entries)

    def publish(self, event: MemoryEvent) -> None:
        """Hand one event to the stream; a failure is a drop with a warning, never a raise.

        The encoder raises for a kind with no wire form and for a free-form field holding a
        value no wire format carries, and Redis raises when it is unreachable. All three are
        the same outcome here — the event is lost and the producing turn is not — because
        the caller is the agent's terminal emit node, which publishes unguarded.
        """
        try:
            payload = json.dumps(encode_memory_event(event))
            self._client.xadd(
                self._stream,
                {PAYLOAD_FIELD: payload},
                maxlen=self._max_entries,
                approximate=True,
            )
        except (redis.exceptions.RedisError, TypeError, ValueError):
            # The scope is what the in-process drop logs; the capacity it logs beside it has
            # no analogue here, because an approximate trim does not report what it removed.
            log.warning(
                "Memory event dropped: %s could not be published to %r scope=%s",
                type(event).__name__,
                self._stream,
                event.scope,
                exc_info=True,
            )


class RedisMemoryEventConsumer(IMemoryEventConsumer):
    """What the background-memory tier reads its work from: one group over the stream.

    The group is created on first use rather than in the constructor, so composing this tier
    does not require a reachable broker — the same shape the agent-event consumer takes, and
    what lets a root be built and inspected without a server.
    """

    def __init__(self, *, client: redis.asyncio.Redis, stream: str, group: str, consumer: str) -> None:
        if not str(stream or "").strip():
            raise ValueError("a redis memory-event consumer requires its stream")
        if not str(group or "").strip():
            raise ValueError("a redis memory-event consumer requires its group")
        if not str(consumer or "").strip():
            raise ValueError("a redis memory-event consumer requires its consumer name")
        self._client = client
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._group_ready = False
        #: The package's own policy: an I/O failure in `poll` is `None` and a paced wait,
        #: never a raise — and the wait is awaited here rather than slept inside the policy.
        self._outage = AsyncBrokerOutage(
            logger=log,
            retry_after_sec=_UNAVAILABLE_RETRY_SEC,
            report_every_sec=_UNAVAILABLE_REPORT_INTERVAL_SEC,
        )
        #: When the pending list may be settled again.
        self._next_maintenance = 0.0

    async def poll(self, timeout: float = 0.0) -> MemoryEvent | None:
        """The next event, or ``None`` once ``timeout`` has passed. Never raises on I/O.

        ``timeout <= 0`` issues no ``BLOCK`` at all, matching ``queue.get_nowait()`` on the
        in-process sibling: ``BLOCK 0`` means *block forever* in Redis, and the loop asks for
        a zero timeout whenever a follow-up is due — with the stop signal re-checked only
        between polls, that would be a process that does not shut down.

        A broker failure returns ``None`` rather than raising. The loop's handler for a
        raising poll is to poll again at once (`background_memory/runtime.py`), and a refused
        connection answers in microseconds, so raising here is a spin that burns a core and
        writes megabytes of tracebacks a second. Instead the caller's remaining timeout is
        spent waiting — which is what the loop would have spent it on — and the next polls
        return immediately, without a command, until `_UNAVAILABLE_RETRY_SEC` has passed.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        if self._outage.stands():
            await wait_out(deadline)
            return None
        try:
            await self._ensure_group()
            await self._maintain()
            event = await self._drain(deadline)
        except redis.exceptions.RedisError as error:
            self._group_ready = False
            self._outage.record(
                "The memory-event stream %r cannot be read (%s); retrying every %.0fs. "
                "Events published during the outage are not recovered.",
                self._stream,
                error,
                _UNAVAILABLE_RETRY_SEC,
            )
            await wait_out(deadline)
            return None
        self._outage.cleared("The memory-event stream %r can be read again", self._stream)
        return event

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except redis.exceptions.RedisError:
            log.warning("Could not close the memory-event consumer of %r", self._stream, exc_info=True)

    async def _drain(self, deadline: float) -> MemoryEvent | None:
        skipped = 0
        while True:
            entry = first_entry(await self._read(deadline))
            if entry is not None:
                event = await self._take(entry)
                if event is not None:
                    return event
                skipped += 1
                if skipped >= MAX_SKIPPED_ENTRIES_PER_POLL:
                    # Back to the loop rather than draining the whole backlog here: the next
                    # poll continues where this one stopped, and the stop signal is checked
                    # in between.
                    return None
                continue
            if time.monotonic() >= deadline:
                return None

    async def _maintain(self) -> None:
        """Settle what the acknowledgement on read did not; throttled, never fails a poll.

        An ``XACK`` that could not reach the broker leaves an entry pending, and nothing else
        in this backend would look at it again: with one fixed consumer name and no reclaim,
        that list only grows. Claiming and acknowledging entries idle past the TTL bounds it.
        It does **not** redeliver: such an entry was decoded and handed to a handler before
        the failed ack, and a redelivered distillation re-runs an LLM tool loop that writes
        memories.
        """
        now = time.monotonic()
        if now < self._next_maintenance:
            return
        self._next_maintenance = now + _MAINTENANCE_INTERVAL_SEC
        try:
            claimed = await self._client.xautoclaim(
                self._stream,
                self._group,
                self._consumer,
                min_idle_time=_PENDING_IDLE_TTL_MS,
                start_id="0-0",
                count=_SETTLE_BATCH,
                justid=True,
            )
            settled = [entry_id for entry_id in claimed or () if entry_id]
            if settled:
                await self._client.xack(self._stream, self._group, *settled)
                log.warning(
                    "Settled %d memory-event entr%s left pending on %r by a failed acknowledgement",
                    len(settled),
                    "y" if len(settled) == 1 else "ies",
                    self._stream,
                )
        except Exception:
            # Never the reason a poll fails: the work in the stream matters more than the
            # bookkeeping, and the next pass tries again.
            log.warning("Memory-event pending settle on %r failed", self._stream, exc_info=True)

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            # From `$`: a tier coming up must not replay every task completed before it
            # existed, which would re-run distillation over the whole retained stream.
            await self._client.xgroup_create(self._stream, self._group, id="$", mkstream=True)
        except redis.exceptions.ResponseError as error:
            if not is_existing_group(error):
                raise
        self._group_ready = True

    async def _read(self, deadline: float) -> Any:
        """One entry, with the one recovery a stateless group needs.

        A Redis restart without persistence takes the group with the stream, and every read
        from then on answers ``NOGROUP``. The group is recreated and the read re-run once; a
        second failure reaches ``poll``, which records the outage. Recreating recovers
        nothing — what was published during the outage is gone.

        The block is derived from ``deadline`` on **each** attempt rather than reused, so the
        retry spends what is left of the caller's timeout instead of a second full one: a
        ``poll(t)`` that recreated its group must still return within ``t``.
        """
        for attempt in (1, 2):
            try:
                return await self._client.xreadgroup(
                    self._group,
                    self._consumer,
                    {self._stream: ">"},
                    count=1,
                    block=block_ms(deadline, int(MAX_POLL_BLOCK_SEC * 1000)) or None,
                )
            except redis.exceptions.ResponseError as error:
                if not is_missing_group(error) or attempt == 2:
                    raise
                log.warning("The %r memory-event group vanished; recreating it", self._group)
                self._group_ready = False
                await self._ensure_group()
        raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover

    async def _take(self, entry: tuple[str, Mapping[Any, Any]]) -> MemoryEvent | None:
        """The decoded event, or ``None`` after acknowledging one nothing can read.

        The acknowledgement goes first in both cases. The interface exposes none
        (``background_memory/interfaces.py``), the loop already treats a failed handling as a
        lost event rather than a retry (``background_memory/runtime.py``), and a redelivered
        distillation would re-run an LLM tool loop that writes memories.
        """
        entry_id, fields = entry
        try:
            await self._client.xack(self._stream, self._group, entry_id)
        except redis.exceptions.RedisError:
            # The entry stays pending until `_maintain` settles it — never redelivered, since
            # it is about to be handled here. Still handed to the loop: the work is in hand,
            # and losing it as well would trade one bookkeeping row for one distillation.
            log.warning("Could not acknowledge memory-event entry %s of %r", entry_id, self._stream)
        payload = row(fields)
        try:
            return decode_memory_event(json.loads(payload.get(PAYLOAD_FIELD) or "null"))
        except UnknownMemoryEventKind as unknown:
            # A newer producer against an older tier: recorded and skipped, as the loop
            # already treats an event kind with no handler.
            log.warning(
                "Skipping memory-event entry %s of %r: this build does not know the kind %r",
                entry_id,
                self._stream,
                str(unknown),
            )
            return None
        except Exception:
            log.warning(
                "Dropping memory-event entry %s of %r: its %s does not decode",
                entry_id,
                self._stream,
                PAYLOAD_FIELD,
                exc_info=True,
            )
            return None


__all__ = [
    "MAX_POLL_BLOCK_SEC",
    "MAX_SKIPPED_ENTRIES_PER_POLL",
    "PAYLOAD_FIELD",
    "REDIS_MAX_STREAM_ENTRIES",
    "RedisMemoryEventConsumer",
    "RedisMemoryEventPublisher",
]
