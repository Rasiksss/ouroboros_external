"""The Redis Streams backend of the memory-event seam.

It runs against two servers, the way the agent-event suite does. `fakeredis` behind the
shared double (`tests/helpers/redis_double.py`) is part of the default gate, so the backend
is not a module CI never executes; a real Redis 7 behind `-m integration` is the acceptance
— point `OUROBOROS_TEST_REDIS_URL` at one and run

    uv run pytest tests/background_memory/test_redis_events.py -m integration

**Where the double is not the server, said here rather than only in its own docstring:**

* `XADD … MAXLEN ~` trims exactly on the double and on node boundaries on a real server, so
  the stream there can sit slightly above the cap. The shared case therefore asserts the
  length as an inequality and a double-only case pins the exact figure; both servers also
  see the assertion that the append *asks* for the bound, which is this module's own
  decision.
* A failed `XACK` and a broker that is not there are injected here rather than caused, so
  what the cases prove is this backend's answer to those replies — the settle pass, the
  outage record and the backoff — and not that a real server produces them the same way.
* Two readers racing for one entry are serialized by the double, so the competing-readers
  case proves that neither drops or duplicates, never how a real server orders them.
* A socket that times out, a connection the server reaps and a `BLOCK` that a real server
  services are outside it entirely.
"""

from __future__ import annotations

import logging
import os
import pathlib
import time
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
import redis

from background_memory.infra.events.codec import KIND_FIELD, encode_memory_event
from background_memory.infra.events.redis_transport import (
    MAX_SKIPPED_ENTRIES_PER_POLL,
    PAYLOAD_FIELD,
    REDIS_MAX_STREAM_ENTRIES,
    RedisMemoryEventConsumer,
    RedisMemoryEventPublisher,
)
from background_memory.interfaces import (
    ChatMessagePersistedEvent,
    IMemoryEventConsumer,
    IMemoryEventPublisher,
    MemoryDistillationEvent,
    MemoryEvent,
)
from tests.helpers.memory_event_conformance import (
    MemoryEventConformance,
    MemoryEventPair,
    distillation_event,
    poll,
)
from tests.helpers.redis_double import async_client_onto, client_onto, fake_server
from tests.helpers.shared import TEST_SCOPE, scope_with

TEST_REDIS_URL = os.environ.get("OUROBOROS_TEST_REDIS_URL", "")

needs_redis = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_REDIS_URL,
        reason="set OUROBOROS_TEST_REDIS_URL to a Redis 7 to run the memory-event suite against a server",
    ),
]

MODULE = "background_memory.infra.events.redis_transport"

#: Key prefixes this module minted, deleted afterwards: a group and its entries would
#: otherwise carry one case's backlog into the next.
_MINTED: list[str] = []

_BROKEN_COMMANDS = ("xreadgroup", "xack", "xadd", "xgroup_create", "xautoclaim")


def _refused_by_a_broker_that_is_gone(*_args, **_kwargs):
    raise redis.exceptions.ConnectionError("the broker is not there")


def _server_client() -> redis.Redis:
    return redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True, socket_connect_timeout=5.0, socket_timeout=10.0)


def _async_server_client():
    import redis.asyncio

    return redis.asyncio.Redis.from_url(
        TEST_REDIS_URL, decode_responses=True, socket_connect_timeout=5.0, socket_timeout=10.0
    )


def _fresh_stream() -> str:
    prefix = f"ouroboros-test:{uuid.uuid4().hex}"
    _MINTED.append(prefix)
    return f"{prefix}:memory-events"


@pytest.fixture(autouse=True)
def _drop_minted_keys():
    yield
    if not TEST_REDIS_URL or not _MINTED:
        _MINTED.clear()
        return
    client = _server_client()
    for prefix in _MINTED:
        keys = client.keys(f"{prefix}*")
        if keys:
            client.delete(*keys)
    _MINTED.clear()


@pytest.fixture
def local_clients():
    """Two clients onto one local keyspace: the publisher's and the consumer's.

    Two because the seam is asymmetric — publishing stays synchronous and consuming is
    awaited — and redis-py binds those to two client classes over one description.
    """
    server = fake_server()
    return client_onto(server), async_client_onto(server)


def _transport(client, async_client, stream: str = "") -> tuple[RedisMemoryEventPublisher, RedisMemoryEventConsumer]:
    """Both halves over one stream, as a deployment composes them in two processes.

    The consumer has **joined** the group: it is created on the first read, at `$`, so an
    event published before that read is not in the tier's history and never arrives. A case
    that wants to observe a publish therefore reads once first, which is what a long-running
    tier does before any producer completes a task.
    """
    stream = stream or _fresh_stream()
    consumer = _consumer(async_client, stream)
    assert poll(consumer) is None
    return (
        RedisMemoryEventPublisher(client=client, stream=stream, max_entries=REDIS_MAX_STREAM_ENTRIES),
        consumer,
    )


def _consumer(async_client, stream: str) -> RedisMemoryEventConsumer:
    return RedisMemoryEventConsumer(
        client=async_client,
        stream=stream,
        group=f"{stream}:background-memory",
        consumer="background-memory",
    )


class _RecordingClient:
    """Every command this module issues, in order, with the kwargs it issued them with."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        inner = getattr(self._inner, name)

        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return inner(*args, **kwargs)

        return _record

    def named(self, name: str) -> list[tuple[str, tuple, dict]]:
        return [call for call in self.calls if call[0] == name]


# -- the suite both servers run -------------------------------------------


class _MemoryRedisCases(MemoryEventConformance):
    """The behaviour of the backend itself; only where the server lives varies."""

    _client: redis.Redis
    _async_client: Any
    _stream: str

    def clients(self):
        raise NotImplementedError("a subclass says which server these run against")

    def make_publisher_and_consumer(self) -> MemoryEventPair:
        self._client, self._async_client = self.clients()
        self._stream = _fresh_stream()
        return _transport(self._client, self._async_client, self._stream)

    def second_consumer(self, consumer: IMemoryEventConsumer) -> IMemoryEventConsumer:
        return _consumer(self._async_client, self._stream)

    def make_group_after_history(self, history: Sequence[MemoryEvent]) -> MemoryEventPair:
        self._client, self._async_client = self.clients()
        self._stream = _fresh_stream()
        publisher = RedisMemoryEventPublisher(
            client=self._client, stream=self._stream, max_entries=REDIS_MAX_STREAM_ENTRIES
        )
        for event in history:
            publisher.publish(event)
        consumer = _consumer(self._async_client, self._stream)
        assert poll(consumer) is None
        return publisher, consumer

    def inject_raw_record(self, consumer: IMemoryEventConsumer, payload: bytes) -> bool:
        self._client.xadd(self._stream, {PAYLOAD_FIELD: payload})
        return True

    def max_skipped_per_poll(self) -> int:
        return MAX_SKIPPED_ENTRIES_PER_POLL

    def serializes_events(self) -> bool:
        return True

    def test_nothing_is_left_pending_after_a_read(self) -> None:
        """The entry is acknowledged before it is handed to the loop."""
        stream = _fresh_stream()
        client, async_client = self.clients()
        publisher, consumer = _transport(client, async_client, stream)
        assert poll(consumer) is None  # the group exists from here on
        publisher.publish(distillation_event())

        assert poll(consumer, 2.0) is not None

        assert client.xpending(stream, f"{stream}:background-memory")["pending"] == 0

    def test_an_undecodable_entry_is_acknowledged_rather_than_left_pending(self) -> None:
        publisher, consumer = self.make_publisher_and_consumer()
        self.inject_raw_record(consumer, b"{not json")
        publisher.publish(distillation_event("the-one-after-it"))

        assert poll(consumer, 2.0) is not None
        assert self._client.xpending(self._stream, f"{self._stream}:background-memory")["pending"] == 0

    def test_consumer_names_do_not_pile_up_in_the_group(self) -> None:
        """One fixed name for the tier, so a group nothing prunes cannot grow.

        The alternative — a name minted per instance, the way the agent-event consumer does
        — would need `XGROUP DELCONSUMER` at shutdown; here it buys nothing, because the
        backend acknowledges before it dispatches, so a consumer holds no pending entry.
        """
        stream = _fresh_stream()
        client, async_client = self.clients()
        for _ in range(5):  # five background-memory processes over the deployment's life
            assert poll(_consumer(async_client, stream)) is None

        names = [str(item["name"]) for item in client.xinfo_consumers(stream, f"{stream}:background-memory")]

        assert names == ["background-memory"]

    def test_an_overflowing_stream_is_trimmed_rather_than_grown(self) -> None:
        """The bound is the broker's own; what a publish may never do is fail the turn.

        The length is asserted as an inequality because the two servers differ exactly here:
        the double trims to the cap on every append, while a real server trims on node
        boundaries and can sit above it until a whole node can go. What both must show is
        that twelve appends against a cap of four are *not* twelve entries kept forever.
        """
        stream = _fresh_stream()
        client, _async_client = self.clients()
        publisher = RedisMemoryEventPublisher(client=client, stream=stream, max_entries=4)

        for index in range(12):
            publisher.publish(ChatMessagePersistedEvent(scope=TEST_SCOPE, message_id=f"message-{index:04d}"))

        assert 4 <= client.xlen(stream) <= 12

    def test_a_group_lost_with_the_brokers_state_is_recreated(self) -> None:
        """A Redis without persistence loses the group with the stream.

        Without this the loop — which swallows what `poll` raises and polls again — would
        warn five times a second and consume nothing for the rest of the process's life.
        """
        stream = _fresh_stream()
        client, async_client = self.clients()
        publisher, consumer = _transport(client, async_client, stream)
        assert poll(consumer) is None
        client.xgroup_destroy(stream, f"{stream}:background-memory")
        publisher.publish(distillation_event("after-the-restart"))

        polled = poll(consumer, 2.0)

        assert polled is None or polled.task.task_id == "after-the-restart"  # type: ignore[union-attr]
        publisher.publish(distillation_event("the-next-one"))
        assert poll(consumer, 2.0) is not None, "the group must have been recreated, not lost for good"


class _LocalDouble:
    """Two clients onto a keyspace of this case's own."""

    _client: redis.Redis

    def clients(self):
        server = fake_server()
        return client_onto(server), async_client_onto(server)

    def break_broker(self, publisher: IMemoryEventPublisher, consumer: IMemoryEventConsumer) -> bool:
        for command in _BROKEN_COMMANDS:
            setattr(self._client, command, _refused_by_a_broker_that_is_gone)
            setattr(self._async_client, command, _refused_by_a_broker_that_is_gone)
        return True

    def heal_broker(self, publisher: IMemoryEventPublisher, consumer: IMemoryEventConsumer) -> None:
        for command in _BROKEN_COMMANDS:
            delattr(self._client, command)
            delattr(self._async_client, command)


class _RealServer:
    """Clients onto the configured test Redis."""

    def clients(self):
        return _server_client(), _async_server_client()

    def break_broker(self, publisher: IMemoryEventPublisher, consumer: IMemoryEventConsumer) -> bool:
        return False


class TestLocalDoubleMemoryEvents(_LocalDouble, _MemoryRedisCases):
    pass


class TestRealRedisMemoryEvents(_RealServer, _MemoryRedisCases):
    pytestmark = needs_redis


# -- the commands this module issues, which are its own decision ----------


def test_a_poll_without_a_timeout_issues_no_block_argument(local_clients) -> None:
    """`BLOCK 0` blocks forever in Redis, and the loop asks for a zero timeout."""
    local_client, local_async_client = local_clients
    recording = _RecordingClient(local_async_client)
    _publisher, consumer = _transport(local_client, recording)

    assert poll(consumer) is None

    reads = recording.named("xreadgroup")
    assert reads, "the poll must reach XREADGROUP"
    assert all(not call[2].get("block") for call in reads)
    assert all(call[2].get("count") == 1 for call in reads)


def test_a_poll_with_a_timeout_blocks_for_at_most_that_long(local_clients) -> None:
    local_client, local_async_client = local_clients
    recording = _RecordingClient(local_async_client)
    _publisher, consumer = _transport(local_client, recording)
    recording.calls.clear()  # the join above is a read of its own

    assert poll(consumer, 0.2) is None

    blocks = [call[2].get("block") for call in recording.named("xreadgroup")]
    assert blocks, "the poll must reach XREADGROUP"
    assert all(0 < block <= 200 for block in blocks), blocks
    assert max(blocks) >= 100, "a timeout must not be spent spinning on unblocked reads"


def test_the_append_asks_for_the_bound_and_never_a_trim_by_age(local_clients) -> None:
    """`MAXLEN ~` is the broker analogue of the bounded queue; the slack is the point.

    Asserted on the command rather than on a length: a real server trims on node
    boundaries, so `XLEN` sits above the cap until a whole node can go.
    """
    local_client, local_async_client = local_clients
    recording = _RecordingClient(local_client)
    publisher, _consumer = _transport(recording, local_async_client)

    publisher.publish(distillation_event())

    appends = recording.named("xadd")
    assert len(appends) == 1
    assert appends[0][2].get("maxlen") == REDIS_MAX_STREAM_ENTRIES
    assert appends[0][2].get("approximate") is True
    assert not appends[0][2].get("minid")


def test_the_entry_is_acknowledged_before_the_event_is_returned(local_clients) -> None:
    local_client, local_async_client = local_clients
    recording = _RecordingClient(local_async_client)
    publisher, consumer = _transport(local_client, recording)
    publisher.publish(distillation_event())

    assert poll(consumer, 2.0) is not None

    issued = [call[0] for call in recording.calls]
    assert issued[-2:] == ["xreadgroup", "xack"], "nothing may be dispatched with the entry still pending"


def test_the_broker_capacity_is_declared_separately_from_the_in_process_one() -> None:
    """32 is sized for a queue a thread is about to drain, not for a stalled tier."""
    import ast

    from background_memory.infra.events.inmemory import MAX_PENDING_EVENTS

    assert REDIS_MAX_STREAM_ENTRIES > MAX_PENDING_EVENTS
    tree = ast.parse(pathlib.Path("background_memory/infra/events/redis_transport.py").read_text(encoding="utf-8"))
    imported = [
        alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in (node.names or ())
    ]
    assert "MAX_PENDING_EVENTS" not in imported, "the two capacities are two constants, not one reused"


def test_a_blocking_poll_never_asks_for_longer_than_the_socket_will_wait() -> None:
    """redis-py raises on a blocking read that is doing exactly what it was asked to do."""
    from background_memory.infra.events import redis_transport
    from infra.redis_client import BROKER_SOCKET_TIMEOUT_SEC

    assert redis_transport.MAX_POLL_BLOCK_SEC < BROKER_SOCKET_TIMEOUT_SEC


# -- what a publish does when the broker is not there ---------------------


class _BrokenClient:
    """A client whose every command fails, as one pointed at a broker that is gone."""

    def __getattr__(self, name: str):
        def _fail(*_args, **_kwargs):
            raise redis.exceptions.ConnectionError("the broker is not there")

        return _fail


def test_a_broker_failure_does_not_fail_the_producing_turn(caplog) -> None:
    """The agent's terminal emit publishes unguarded: a raise here fails a completed turn."""
    publisher = RedisMemoryEventPublisher(client=_BrokenClient(), stream="s:memory-events", max_entries=8)

    with caplog.at_level(logging.WARNING, logger=MODULE):
        assert publisher.publish(distillation_event()) is None

    assert any("MemoryDistillationEvent" in record.getMessage() for record in caplog.records)
    assert any("scope=" in record.getMessage() for record in caplog.records)


def test_a_read_that_finds_the_group_gone_recreates_it_and_returns_the_next_event(local_clients, caplog) -> None:
    """One `NOGROUP` must not become a tier that has stopped consuming.

    The failure is injected rather than produced by destroying the group, so the case
    pins the retry itself: the read that answered `NOGROUP` is re-run once, and the entry
    it would have delivered is delivered.
    """
    local_client, local_async_client = local_clients
    stream = _fresh_stream()
    publisher, consumer = _transport(local_client, local_async_client, stream)
    assert poll(consumer) is None
    publisher.publish(distillation_event("after-the-restart"))
    reads = {"count": 0}
    inner_read = local_async_client.xreadgroup

    def _one_nogroup(*args, **kwargs):
        reads["count"] += 1
        if reads["count"] == 1:
            raise redis.exceptions.ResponseError("NOGROUP No such consumer group")
        return inner_read(*args, **kwargs)

    local_async_client.xreadgroup = _one_nogroup  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger=MODULE):
        polled = poll(consumer, 1.0)

    assert isinstance(polled, MemoryDistillationEvent)
    assert polled.task.task_id == "after-the-restart"
    assert any("vanished" in record.getMessage() for record in caplog.records)


def test_the_double_trims_to_the_cap_on_every_append(local_clients) -> None:
    """What the shared case above cannot assert, pinned where it is true.

    `fakeredis` implements `MAXLEN ~` as an exact trim, so the bound is observable here to
    the entry; on a real server the same publishes leave the stream above the cap until a
    whole radix node can go, which is the slack the `~` buys and the reason the shared case
    is an inequality.
    """
    local_client, _local_async_client = local_clients
    stream = _fresh_stream()
    publisher = RedisMemoryEventPublisher(client=local_client, stream=stream, max_entries=4)

    for index in range(12):
        publisher.publish(ChatMessagePersistedEvent(scope=TEST_SCOPE, message_id=f"message-{index:04d}"))

    assert local_client.xlen(stream) == 4


def test_a_broker_that_is_not_there_does_not_become_a_hot_loop(caplog) -> None:
    """The loop re-polls immediately after a failed poll, and a refused connection is instant.

    Measured rather than argued: a raising `poll` was ~15 000 attempts and ~11 000 warnings
    with tracebacks in two seconds — a core and megabytes of log a second — because
    `MemoryRuntime.execute` catches what `poll` raises and polls again with no delay. So the
    failure is recorded once, the caller's timeout is spent waiting, and the polls that
    follow issue no command at all until the retry interval has passed.
    """
    from background_memory.infra.events import redis_transport

    attempts = {"count": 0}

    class _CountingBrokenClient(_BrokenClient):
        def __getattr__(self, name: str):
            attempts["count"] += 1
            return super().__getattr__(name)

    consumer = _consumer(_CountingBrokenClient(), "s:memory-events")

    with caplog.at_level(logging.WARNING, logger=MODULE):
        started = time.monotonic()
        for _ in range(50):  # what the loop would do in one tenth of a second
            assert poll(consumer, 0.01) is None
        elapsed = time.monotonic() - started

    assert elapsed >= 0.4, "each poll must spend the timeout the loop meant to spend"
    assert attempts["count"] <= 2, "a broker known to be down must not be dialled once per poll"
    assert len(caplog.records) == 1, "one outage is one report, not one per attempt"
    assert redis_transport._UNAVAILABLE_RETRY_SEC > 0


def test_an_entry_left_pending_by_a_failed_ack_is_settled_rather_than_kept(local_clients, monkeypatch, caplog) -> None:
    """ "Empty by construction" is false where the ack itself fails, so something settles it.

    Without this the pending list grows for the life of a deployment: one fixed consumer
    name, no reclaim, and nothing in the backend ever looks at those entries again.
    """
    local_client, local_async_client = local_clients
    from background_memory.infra.events import redis_transport

    monkeypatch.setattr(redis_transport, "_PENDING_IDLE_TTL_MS", 0)
    stream = _fresh_stream()
    group = f"{stream}:background-memory"
    publisher, consumer = _transport(local_client, local_async_client, stream)
    for index in range(5):
        publisher.publish(distillation_event(f"src{index}"))

    def _dead_ack(*_args, **_kwargs):
        raise redis.exceptions.ConnectionError("the ack never reached the broker")

    monkeypatch.setattr(local_async_client, "xack", _dead_ack)
    while poll(consumer, 0.2) is not None:
        pass
    assert local_client.xpending(stream, group)["pending"] == 5, "the case must reproduce the leak first"

    monkeypatch.undo()
    monkeypatch.setattr(redis_transport, "_PENDING_IDLE_TTL_MS", 0)
    monkeypatch.setattr(redis_transport, "_MAINTENANCE_INTERVAL_SEC", 0.0)
    consumer._next_maintenance = 0.0  # the throttle was armed by the polls above
    with caplog.at_level(logging.WARNING, logger=MODULE):
        poll(consumer, 0.05)

    assert local_client.xpending(stream, group)["pending"] == 0
    assert any("Settled" in record.getMessage() for record in caplog.records)


def test_the_settle_pass_never_fails_a_poll(local_clients, monkeypatch, caplog) -> None:
    """A server without `XAUTOCLAIM`, or an ACL that refuses it, must not stop the tier."""
    local_client, local_async_client = local_clients
    from background_memory.infra.events import redis_transport

    monkeypatch.setattr(redis_transport, "_MAINTENANCE_INTERVAL_SEC", 0.0)
    stream = _fresh_stream()
    publisher, consumer = _transport(local_client, local_async_client, stream)
    publisher.publish(distillation_event("delivered-anyway"))

    def _refused(*_args, **_kwargs):
        raise redis.exceptions.ResponseError("ERR unknown command 'XAUTOCLAIM'")

    monkeypatch.setattr(local_async_client, "xautoclaim", _refused)
    with caplog.at_level(logging.WARNING, logger=MODULE):
        polled = poll(consumer, 1.0)

    assert isinstance(polled, MemoryDistillationEvent)
    assert any("settle" in record.getMessage() for record in caplog.records)


def test_a_poll_that_recreated_its_group_still_returns_within_its_timeout(local_clients) -> None:
    """`poll(t)` may not take `2t`: the retry re-slices against the caller's deadline."""
    local_client, local_async_client = local_clients
    reads = {"count": 0}
    inner_read = local_async_client.xreadgroup

    def _one_nogroup(*args, **kwargs):
        reads["count"] += 1
        if reads["count"] == 1:
            raise redis.exceptions.ResponseError("NOGROUP No such consumer group")
        return inner_read(*args, **kwargs)

    _publisher, consumer = _transport(local_client, local_async_client)
    local_async_client.xreadgroup = _one_nogroup  # type: ignore[method-assign]

    started = time.monotonic()
    assert poll(consumer, 0.3) is None
    elapsed = time.monotonic() - started

    assert elapsed < 0.55, "the recreated read must spend what is left of the timeout, not a second one"


def test_an_acknowledgement_that_fails_still_delivers_the_event(local_clients, caplog) -> None:
    """The work is in hand; losing it as well would trade one bookkeeping row for a distillation.

    What it costs is stated where it happens: the entry stays in this consumer's pending
    list.
    """
    local_client, local_async_client = local_clients
    publisher, consumer = _transport(local_client, local_async_client)
    publisher.publish(distillation_event("acked-nowhere"))

    def _dead_ack(*_args, **_kwargs):
        raise redis.exceptions.ConnectionError("the ack never reached the broker")

    local_async_client.xack = _dead_ack  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger=MODULE):
        polled = poll(consumer, 1.0)

    assert isinstance(polled, MemoryDistillationEvent)
    assert polled.task.task_id == "acked-nowhere"
    assert any("acknowledge" in record.getMessage() for record in caplog.records)


def test_both_halves_require_the_keys_they_work_on() -> None:
    with pytest.raises(ValueError, match="stream"):
        RedisMemoryEventPublisher(client=_BrokenClient(), stream="", max_entries=8)
    with pytest.raises(ValueError, match="capacity"):
        RedisMemoryEventPublisher(client=_BrokenClient(), stream="s:memory-events", max_entries=0)
    with pytest.raises(ValueError, match="group"):
        RedisMemoryEventConsumer(client=_BrokenClient(), stream="s:memory-events", group="", consumer="c")
    with pytest.raises(ValueError, match="consumer"):
        RedisMemoryEventConsumer(client=_BrokenClient(), stream="s:memory-events", group="g", consumer="")


def test_the_kind_travels_in_the_payload_this_module_writes(local_clients) -> None:
    """One stream field, so the entry is exactly what the codec produced."""
    local_client, local_async_client = local_clients
    stream = _fresh_stream()
    publisher, _consumer = _transport(local_client, local_async_client, stream)

    publisher.publish(ChatMessagePersistedEvent(scope=scope_with(TEST_SCOPE, owner_id="someone-else"), message_id="m"))

    entries = local_client.xrange(stream)
    assert len(entries) == 1
    assert set(entries[0][1]) == {PAYLOAD_FIELD}
    assert (
        encode_memory_event(ChatMessagePersistedEvent(scope=TEST_SCOPE, message_id="m"))[KIND_FIELD]
        in (entries[0][1][PAYLOAD_FIELD])
    )
