"""The multi-instance backend of the agent-event seam: one Redis stream, both halves.

Imported by composition roots alone — neither a run nor the supervisor names this module,
exactly as with its in-process sibling ``ouroboros/agent_events/inmemory.py``. What it adds
over that one is the wire: ``ouroboros/agent_events/codec.py`` owns the format, so a report
arrives as the kind it was published as rather than as an equal collection of JSON values.

These decisions explain most of the code below.

* **A broker that is not there is never raised out of `poll`.** This consumer is polled from
  inside the supervisor's tick, so returning immediately would make the loop poll again without
  pacing. The policy is
  ``ouroboros/contracts/stream_replies.py::BrokerOutage``, shared with the other two stream
  readers so the three cannot answer this question three ways again.

* **`NOACK`, and the entry is deleted on read.** The seam's consumer has one method,
  ``poll(timeout)``, and no acknowledgement, so a pending-entries list nobody can settle
  would grow for the life of the deployment and count against the bound forever. Delivery
  is therefore at-most-once, which is what the seam already promises: losing a report costs
  a display update, an accounting row or an echo, never the completion of a task. ``XACK``
  cannot be what removes an entry — a stream keeps entries after acknowledgement — so the
  consumer issues ``XDEL`` *before* it returns the decoded report.
* **A delete that never happened is swept, so the bound cannot decay.** ``XDEL`` is a second
  command after the read, not part of it: a process that dies in between, or a delete that
  fails, leaves an entry that ``NOACK`` already made undeliverable sitting in the stream
  forever — and since the publisher gates on ``XLEN``, every such entry would permanently
  lower the cap until the fleet wedged against a backlog nothing could drain. The consumer
  therefore trims on a throttled pass to the floor every group has already been delivered
  (``XTRIM MINID``), which by definition removes only entries no reader can still receive.
  ``XDEL`` stays as the fast path that keeps ``XLEN`` honest between passes; the trim is
  what makes it a bound rather than a ratchet.
* **The stream is bounded by back-pressure at publish, never by `MAXLEN`.** A trim by age
  discards the entry that has waited longest to be delivered, which is overwhelmingly the
  owner's answer and never the typing indicator that supersedes itself — exactly the kind
  the seam forbids dropping. Above the cap a droppable kind is dropped with a warning and
  every other kind waits, which is what the in-process backend already does.
* **A publish that cannot reach the broker raises, and so does one that waits too long.**
  *Full* is back-pressure and waits; *unreachable* is a connection error, and waiting inside
  ``publish`` for a broker that is gone does not bring it back. The wait itself is bounded by
  ``MAX_BACKPRESSURE_WAIT_SEC``, because an unbounded one is not back-pressure but a process
  that cannot be shut down: these publishes run on ``ThreadPoolExecutor`` worker threads,
  which are not daemons and are joined at interpreter exit, so a supervisor tier that stopped
  draining would leave every runtime unable to answer ``SIGTERM``. At the bound the report is
  refused with ``AgentEventBacklogFull`` rather than discarded silently. The call sites guard
  their own publishes (``ouroboros/agent_runtime/executor.py``, ``ouroboros/agent/core.py``),
  so a raise costs a log line rather than the outcome of the run being reported.
* **The group starts at `0`, not `$`.** The stream holds only what nobody has drained, so a
  supervisor coming up must take the backlog; starting at the present would strand answers
  already published and leave the publishers' back-pressure permanently engaged.

**Declared deviation: per-scope delivery order is not preserved.** Within the stream
``XADD`` totally orders entries, so publication order for one scope is well defined; what a
consumer group does not give is delivery in that order. ``XREADGROUP`` hands different
entries to different consumers, so with several supervisor instances in one group two
reports of one scope are handled by two processes at once. What a reader loses is display
order: a progress line can arrive after the answer it preceded, ``TypingStopped`` before the
answer (the indicator then stays on until the transport times it out), and ``TaskFinished``
before the message the live card waits for — ``supervisor/events.py`` persists the finish
there rather than in the run precisely so the reply reaches the UI first. It costs order,
never content: every report still arrives and the answer is in the task record regardless.
**A backend that can order by the partition key must not take this deviation** — a keyed
producer on a partitioned broker removes it rather than declaring it, which is why both
halves still answer ``partition_key``.

Crash tolerance is out of scope by decision: no ``XAUTOCLAIM``, no failover, no persistence.
An entry read by an instance that then dies is lost, and a Redis restart empties the stream.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping
from typing import Any

import redis

from new_core.identity import OwnerScope
from ouroboros.agent_events.codec import (
    KIND_FIELD,
    PAYLOAD_FIELD,
    UnknownAgentEventKind,
    decode_agent_event,
    encode_agent_event,
    ensure_wire_safe,
)
from ouroboros.agent_events.interfaces import (
    MAX_BACKPRESSURE_WAIT_SEC,
    AgentEvent,
    AgentEventBacklogFull,
    IAgentEventConsumer,
    IAgentEventPublisher,
)
from ouroboros.contracts.stream_replies import (
    BROKER_REPORT_INTERVAL_SEC,
    BROKER_RETRY_SEC,
    BrokerOutage,
    block_ms,
    entry_key,
    first_entry,
    is_existing_group,
    is_missing_group,
    next_entry_id,
    row,
)

log = logging.getLogger(__name__)

#: How many reports the stream holds before a publish has to wait. The in-process cap is
#: 512 per process, "sized for a burst of one run's live logs and beats while the consumer
#: is busy with a slow handler, times a few concurrent runs"
#: (`ouroboros/agent_events/inmemory.py`); this stream carries every runtime instance at
#: once, so it is that figure times the instances a deployment is expected to run.
#: Deliberately a constant and not a settings key: the number is derived, and the
#: derivation belongs beside it.
_EXPECTED_RUNTIME_INSTANCES = 8
MAX_PENDING_STREAM_EVENTS = 512 * _EXPECTED_RUNTIME_INSTANCES

#: The longest a single blocking read asks Redis to wait. Below the socket timeout the
#: composition root configures (`ouroboros/app/broker_composition.py`), or redis-py raises
#: on a blocking read that is doing exactly what it was asked to do. A longer wait is
#: served by repeating the read until the caller's own deadline.
MAX_POLL_BLOCK_SEC = 5.0

#: How often the length is re-read while a publish waits for room.
_BACKLOG_POLL_SEC = 0.05

#: How often a waiting publish says what it is waiting on. A supervisor tier that stopped
#: draining is then legible from every runtime's own logs rather than only from Redis, and
#: the interval is what keeps a normal moment of back-pressure from becoming a log flood.
_BACKLOG_REPORT_INTERVAL_SEC = 30.0

#: Bounded retry for the length probe, which is a read and may be repeated freely. The
#: append is deliberately not retried: a repeated `XADD` whose first reply was lost
#: publishes the owner's answer twice, and nothing downstream can recognise the duplicate.
_LENGTH_RETRIES = 3
_LENGTH_RETRY_BACKOFF_SEC = 0.1

#: How often the consumer sweeps: the trim that keeps the bound from decaying, and the
#: reap of consumer names left behind by instances that are gone. Both cost round trips
#: where nothing else would, so each pass is gated.
_MAINTENANCE_INTERVAL_SEC = 5.0

#: How many unreadable entries one poll may drop before it returns to the tick. The skip has
#: to exist — returning `None` for one bad entry would end the tick's drain with reports still
#: queued behind it — but it must be bounded: the skip is a `continue` past the deadline
#: check, so without a cap one poll owns the supervisor's thread for as long as a backlog of
#: bad entries takes to drain. Its sibling on the memory seam has the same number and the
#: same reason (`background_memory/infra/events/redis_transport.py`).
MAX_SKIPPED_ENTRIES_PER_POLL = 32

#: How long a consumer name may sit idle in the group before it is removed. Under `NOACK`
#: a consumer holds no pending entry, so removing one destroys nothing — which is why the
#: refusal `transport/infra/redis_gateway.py` records for `XGROUP DELCONSUMER` does not
#: apply here. Well above the longest gap between two supervisor ticks.
_CONSUMER_IDLE_TTL_MS = 600_000


class RedisAgentEventPublisher(IAgentEventPublisher):
    """What a run reports through when the consumer is another process.

    Holds the length Redis last reported and gates on it, so the common case is one round
    trip per report: ``XADD`` and ``XLEN`` pipelined together, the returned length being
    what the next publish gates on.
    """

    def __init__(self, *, client: redis.Redis, stream: str) -> None:
        if not str(stream or "").strip():
            raise ValueError("a redis agent-event publisher requires its stream")
        self._client = client
        self._stream = stream
        #: What the last append saw. Zero until the first one, so a fresh publisher
        #: appends once before it can gate — the append itself reports the backlog.
        self._backlog = 0

    def publish(self, event: AgentEvent) -> None:
        if not isinstance(event, AgentEvent):
            raise TypeError(f"the agent-event transport takes an AgentEvent, got {type(event).__name__}")
        # Before anything is written, as in the in-process sibling: a refused report must
        # be refused where the call site still names the producer.
        ensure_wire_safe(event)
        if self._backlog >= MAX_PENDING_STREAM_EVENTS and not self._wait_for_room(event):
            return
        self._append(event)

    @staticmethod
    def partition_key(event: AgentEvent) -> OwnerScope:
        """What delivery would be ordered by, read off the base type without naming a kind.

        Answered even though this backend declares it does not order by it: the answer is
        what a keyed producer on a partitioned broker uses to remove the deviation.
        """
        return event.scope

    def _append(self, event: AgentEvent) -> None:
        """One report onto the stream, and the backlog the next publish gates on.

        No ``MAXLEN``: the bound is applied before a report is accepted, never by evicting
        one already accepted.
        """
        entry = encode_agent_event(event)
        pipeline = self._client.pipeline(transaction=False)
        # Written as a literal of the two fields the codec names, because redis-py types the
        # field map by the call site rather than by what a helper returns.
        pipeline.xadd(self._stream, {KIND_FIELD: entry[KIND_FIELD], PAYLOAD_FIELD: entry[PAYLOAD_FIELD]})
        pipeline.xlen(self._stream)
        self._backlog = int(pipeline.execute()[1] or 0)

    def _wait_for_room(self, event: AgentEvent) -> bool:
        """Whether to publish ``event`` now; ``False`` means it was dropped as droppable.

        The cached length is stale by construction — the consumer deletes on read — so the
        first thing this does is ask. A kind the seam forbids dropping waits, and the wait
        has a deadline: past it the publish is refused rather than held forever, because the
        thread it would hold is one the process must be able to join to exit.
        """
        if self._length() < MAX_PENDING_STREAM_EVENTS:
            return True
        if type(event).droppable:
            log.warning(
                "Dropped a droppable %s: transport full (cap=%d) scope=%s",
                type(event).__name__,
                MAX_PENDING_STREAM_EVENTS,
                self.partition_key(event),
            )
            return False
        # The run waits, which is the seam's stated intent and not a failure: a run that
        # cannot report its answer must not race ahead of its own output.
        deadline = time.monotonic() + MAX_BACKPRESSURE_WAIT_SEC
        next_report = time.monotonic() + _BACKLOG_REPORT_INTERVAL_SEC
        while True:
            time.sleep(_BACKLOG_POLL_SEC)
            length = self._length()
            if length < MAX_PENDING_STREAM_EVENTS:
                return True
            if time.monotonic() >= next_report:
                next_report = time.monotonic() + _BACKLOG_REPORT_INTERVAL_SEC
                log.warning(
                    "Still waiting for room on %r to report a %s: %d entries against a cap of %d. "
                    "Nothing is draining the agent-event stream.",
                    self._stream,
                    type(event).__name__,
                    length,
                    MAX_PENDING_STREAM_EVENTS,
                )
            if time.monotonic() >= deadline:
                raise AgentEventBacklogFull(
                    f"{self._stream!r} held {length} entries against a cap of "
                    f"{MAX_PENDING_STREAM_EVENTS} for {MAX_BACKPRESSURE_WAIT_SEC:.0f}s, so a "
                    f"{type(event).__name__} could not be published. Nothing is draining it."
                )

    def _length(self) -> int:
        """The backlog, with the bounded retry a read may have; a raise reaches the caller."""
        delay = _LENGTH_RETRY_BACKOFF_SEC
        for attempt in range(1, _LENGTH_RETRIES + 1):
            try:
                self._backlog = int(self._client.xlen(self._stream) or 0)
                return self._backlog
            except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
                if attempt == _LENGTH_RETRIES:
                    raise
                time.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


class RedisAgentEventConsumer(IAgentEventConsumer):
    """What the supervisor tier reads reports from: one consumer group over the stream.

    The consumer name is a ``uuid4`` hex minted here. Under ``NOACK`` it is diagnostic
    only — there is no per-consumer state to recover — and it is random rather than a
    hostname or a pid for the reason ``ouroboros/agent_runtime/identity.py`` already gives
    for a runtime id: a pid recycles, and a hostname is shared by two replicas on one node.
    """

    def __init__(self, *, client: redis.Redis, stream: str, group: str) -> None:
        if not str(stream or "").strip():
            raise ValueError("a redis agent-event consumer requires its stream")
        if not str(group or "").strip():
            raise ValueError("a redis agent-event consumer requires its group")
        self._client = client
        self._stream = stream
        self._group = group
        self._consumer = uuid.uuid4().hex
        self._group_ready = False
        #: When the next housekeeping pass may run. A plain deadline rather than a helper
        #: class: there is one gate here.
        self._next_maintenance = 0.0
        #: The policy the three stream readers share: an I/O failure in `poll` is `None` and
        #: a paced wait, never a raise.
        self._outage = BrokerOutage(
            logger=log,
            retry_after_sec=BROKER_RETRY_SEC,
            report_every_sec=BROKER_REPORT_INTERVAL_SEC,
        )

    def poll(self, timeout: float = 0.0) -> AgentEvent | None:
        """The next report, or ``None`` once ``timeout`` has passed. Never raises on I/O.

        This poll runs inside the supervisor's tick (``supervisor/runtime.py``). The broker
        failure is recorded, the caller's remaining timeout is spent
        waiting, and the polls that follow issue no command at all until the retry interval
        has passed. Nothing is lost by waiting: the stream keeps what nobody has taken.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        if self._outage.stands(deadline):
            return None
        try:
            self._ensure_group()
            self._maintain()
            event = self._drain(deadline)
        except redis.exceptions.RedisError as error:
            # No `NOGROUP` branch here, unlike the gateway's inbox: the one command that can
            # answer it is `_read`, which owns that recovery itself, and `_take`'s delete
            # already swallows a failure of its own.
            self._group_ready = False
            self._outage.record(
                deadline,
                "The agent-event stream %r cannot be read (%s); retrying every %.0fs. The tick "
                "keeps running and the reports already in the stream are delivered when it "
                "answers again.",
                self._stream,
                error,
                BROKER_RETRY_SEC,
            )
            return None
        self._outage.cleared("The agent-event stream %r can be read again", self._stream)
        return event

    def _drain(self, deadline: float) -> AgentEvent | None:
        skipped = 0
        while True:
            entry = first_entry(self._read(block_ms(deadline, int(MAX_POLL_BLOCK_SEC * 1000))))
            if entry is not None:
                decoded = self._take(entry)
                if decoded is not None:
                    return decoded
                # An entry nobody can read must not end the drain: the supervisor's tick
                # stops at the first `None`, so every report behind it would wait a tick.
                skipped += 1
                if skipped >= MAX_SKIPPED_ENTRIES_PER_POLL:
                    # Back to the tick rather than draining the whole backlog here: this
                    # `continue` is past the deadline check, so an unbounded skip is one poll
                    # holding the supervisor's thread for as long as the backlog lasts.
                    return None
                continue
            if time.monotonic() >= deadline:
                return None

    @staticmethod
    def partition_key(event: AgentEvent) -> OwnerScope:
        """What delivery would be ordered by, read off the base type without naming a kind."""
        return event.scope

    def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            # From `0`, not `$`: what a runtime published before this tier came up is
            # exactly the backlog whose delivery releases the publishers' back-pressure.
            self._client.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except redis.exceptions.ResponseError as error:
            if not is_existing_group(error):
                raise
        self._group_ready = True

    def _maintain(self) -> None:
        """The housekeeping a stream with no acknowledgement needs; throttled, and never
        able to fail a poll.

        One guard each rather than one around both, for the reason the gateway records:
        sharing a guard lets a step that always fails — an ACL without `XGROUP`, a read-only
        replica — suppress the other silently.
        """
        now = time.monotonic()
        if now < self._next_maintenance:
            return
        self._next_maintenance = now + _MAINTENANCE_INTERVAL_SEC
        for step, task in (("trim", self._trim_delivered), ("consumer reap", self._reap_departed_consumers)):
            try:
                task()
            except Exception:
                log.warning("Agent-event %s on %r failed", step, self._stream, exc_info=True)

    def _trim_delivered(self) -> None:
        """Remove what every group has already been handed; the bound's real guarantee.

        `XDEL` on read is a second command after the read, so an instance that dies in
        between — or a delete that fails — leaves an entry `NOACK` already made
        undeliverable. Nothing else would ever remove it: it is not in any pending list, it
        is behind every group's last-delivered id, and the publisher counts it in `XLEN`
        forever. A handful of restarts would then lower the cap to nothing and wedge every
        runtime against a backlog no consumer can drain.

        The floor is the *lowest* last-delivered id across the groups on the stream, so a
        second logical reader that is behind still gets its entries: only what every group
        has been handed is removed.
        """
        groups = [row(group) for group in self._client.xinfo_groups(self._stream)]
        if not groups:
            return
        delivered = [str(group.get("last-delivered-id") or "0-0") for group in groups]
        floor = min(delivered, key=entry_key)
        if entry_key(floor) == (0, 0):
            return  # nothing has been delivered yet, so nothing may go
        self._client.xtrim(self._stream, minid=next_entry_id(floor))

    def _reap_departed_consumers(self) -> None:
        """Remove consumer names whose instance is gone.

        Every instance mints a fresh name, so without this the group accumulates one entry
        per supervisor process that ever started. `XGROUP DELCONSUMER` is refused on the
        message gateway because it destroys pending entries outright — under `NOACK` there
        are none, which is what makes it safe here. Never this consumer's own name, and
        never one that somehow holds a pending entry.
        """
        for consumer in (row(item) for item in self._client.xinfo_consumers(self._stream, self._group)):
            name = str(consumer.get("name") or "")
            if not name or name == self._consumer:
                continue
            if int(consumer.get("pending") or 0) or int(consumer.get("idle") or 0) < _CONSUMER_IDLE_TTL_MS:
                continue
            self._client.xgroup_delconsumer(self._stream, self._group, name)

    def _read(self, block_for_ms: int) -> Any:
        """One entry, never redelivered: `NOACK` settles it as it is handed over."""
        try:
            return self._client.xreadgroup(
                self._group,
                self._consumer,
                {self._stream: ">"},
                count=1,
                block=block_for_ms or None,
                noack=True,
            )
        except redis.exceptions.ResponseError as error:
            if not is_missing_group(error):
                raise
            # A Redis restart takes every group with it. Recreated rather than raising for
            # the rest of this process's life: the supervisor's tick would crash on every
            # poll and the reports still in the stream would reach nobody.
            log.warning("The %r agent-event group vanished; recreating it", self._group)
            self._group_ready = False
            self._ensure_group()
            return None

    def _take(self, entry: tuple[str, Mapping[Any, Any]]) -> AgentEvent | None:
        """The decoded report, or ``None`` after dropping one nothing can read.

        The delete goes first in both cases: with ``NOACK`` the entry is already
        unrecoverable, so nothing is lost by it, and the sooner it leaves the stream the
        sooner the publisher's gate sees the room. A delete that fails does not fail the
        poll — the report in hand is still owed to a handler — and the trim in ``_maintain``
        is what collects the entry it left behind.
        """
        entry_id, fields = entry
        try:
            self._client.xdel(self._stream, entry_id)
        except redis.exceptions.RedisError:
            log.warning("Could not delete agent-event entry %s of %r; the trim will", entry_id, self._stream)
        payload = row(fields)
        try:
            return decode_agent_event(payload)
        except UnknownAgentEventKind:
            # A newer runtime publishing to an older supervisor. Recorded and skipped, as
            # `dispatch_event` treats a kind with no handler.
            log.warning(
                "Skipping agent-event entry %s of %r: this build does not know the kind %r",
                entry_id,
                self._stream,
                payload.get(KIND_FIELD),
            )
            return None
        except Exception:
            log.error(
                "Dropping agent-event entry %s of %r: its %s does not decode",
                entry_id,
                self._stream,
                PAYLOAD_FIELD,
                exc_info=True,
            )
            return None


__all__ = [
    "MAX_PENDING_STREAM_EVENTS",
    "MAX_POLL_BLOCK_SEC",
    "MAX_SKIPPED_ENTRIES_PER_POLL",
    "RedisAgentEventConsumer",
    "RedisAgentEventPublisher",
]
