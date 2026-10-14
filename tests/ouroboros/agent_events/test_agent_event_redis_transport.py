"""The Redis Streams backend of the agent-event seam.

It runs against two servers. `fakeredis` behind the shared double
(`tests/helpers/redis_double.py`) is part of the default gate, so the backend is not a
module CI never executes; a real Redis 7 behind `-m integration` is the acceptance —
point `OUROBOROS_TEST_REDIS_URL` at one and run
``uv run pytest tests/ouroboros/agent_events/test_agent_event_redis_transport.py -m integration``.

What the double does not prove is in its own docstring. The one thing worth repeating
here: it says nothing about how two live readers in one group race for an entry, which
is exactly the situation this backend declares it does not order.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid

import pytest
import redis

from ouroboros.agent_events.interfaces import AgentEventBacklogFull, OutboundText, TaskHeartbeat
from ouroboros.agent_events.redis_transport import (
    MAX_SKIPPED_ENTRIES_PER_POLL,
    RedisAgentEventConsumer,
    RedisAgentEventPublisher,
)
from tests.helpers.agent_event_conformance import AgentEventConformance
from tests.helpers.redis_double import client_onto, fake_server
from tests.helpers.shared import TEST_SCOPE, scope_with

TEST_REDIS_URL = os.environ.get("OUROBOROS_TEST_REDIS_URL", "")

needs_redis = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_REDIS_URL,
        reason="set OUROBOROS_TEST_REDIS_URL to a Redis 7 to run the agent-event conformance suite",
    ),
]

#: Key prefixes this module minted, deleted afterwards: a group and its entries would
#: otherwise carry one case's backlog into the next.
_MINTED: list[str] = []


def _server_client():
    return redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True, socket_connect_timeout=5.0, socket_timeout=10.0)


def _fresh_stream() -> str:
    prefix = f"ouroboros-test:{uuid.uuid4().hex}"
    _MINTED.append(prefix)
    return f"{prefix}:agent-events"


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


class _RealServer:
    """Clients onto the configured test Redis."""

    def client(self):
        return _server_client()


class _LocalDouble:
    """A client onto a keyspace of this case's own."""

    def client(self):
        return client_onto(fake_server())


class _RedisTransportConformance(AgentEventConformance):
    """The seam's suite unchanged; only where the server lives varies between subclasses."""

    def make_transport(self) -> tuple[RedisAgentEventPublisher, RedisAgentEventConsumer]:
        return _transport(self.client())

    def shrink_capacity(self, monkeypatch) -> int:
        """The bound is a module constant here, so the case can reach it cheaply."""
        monkeypatch.setattr("ouroboros.agent_events.redis_transport.MAX_PENDING_STREAM_EVENTS", 1)
        return 1

    def preserves_order_within_one_scope(self) -> bool:
        """The declared deviation: `XREADGROUP` hands different entries to different readers.

        With exactly one reader in the group the order is preserved, and nothing in the
        deployment enforces exactly one — which is the point of the declaration. The double
        proves nothing either way here: it has one reader by construction.
        """
        return False


class TestLocalDoubleTransport(_LocalDouble, _RedisTransportConformance):
    pass


class TestRedisTransport(_RealServer, _RedisTransportConformance):
    pytestmark = needs_redis


def _transport(client, stream: str = "") -> tuple[RedisAgentEventPublisher, RedisAgentEventConsumer]:
    """Both halves over one stream, as a deployment composes them in two processes."""
    stream = stream or _fresh_stream()
    return (
        RedisAgentEventPublisher(client=client, stream=stream),
        RedisAgentEventConsumer(client=client, stream=stream, group=f"{stream}:supervisor"),
    )


@pytest.fixture
def local_client():
    """A client onto one local keyspace, so both halves meet on the same stream."""
    return client_onto(fake_server())


# -- what a report is when it arrives -------------------------------------


def test_a_published_message_round_trips_intact(local_client) -> None:
    publisher, consumer = _transport(local_client)
    event = OutboundText(scope=TEST_SCOPE, text="answer", transport={"target_uid": "alice"})

    publisher.publish(event)
    polled = consumer.poll()

    assert isinstance(polled, OutboundText)
    assert polled == event
    assert polled.transport == {"target_uid": "alice"}


# -- the bound ------------------------------------------------------------


def test_a_full_stream_drops_a_droppable_kind_without_blocking(local_client, monkeypatch, caplog) -> None:
    monkeypatch.setattr("ouroboros.agent_events.redis_transport.MAX_PENDING_STREAM_EVENTS", 1)
    publisher, consumer = _transport(local_client)
    publisher.publish(OutboundText(scope=TEST_SCOPE, text="the answer"))

    with caplog.at_level(logging.WARNING, logger="ouroboros.agent_events.redis_transport"):
        publisher.publish(TaskHeartbeat(scope=TEST_SCOPE, task_id="t-1"))  # must not block

    assert any("TaskHeartbeat" in record.getMessage() for record in caplog.records)
    assert consumer.poll().text == "the answer"
    assert consumer.poll() is None, "a dropped beat must never have been published"


def test_a_full_stream_blocks_rather_than_dropping_an_answer(local_client, monkeypatch, caplog) -> None:
    monkeypatch.setattr("ouroboros.agent_events.redis_transport.MAX_PENDING_STREAM_EVENTS", 1)
    monkeypatch.setattr("ouroboros.agent_events.redis_transport._BACKLOG_REPORT_INTERVAL_SEC", 0.0)
    publisher, consumer = _transport(local_client)
    publisher.publish(OutboundText(scope=TEST_SCOPE, text="first"))
    delivered = threading.Event()

    def _publish_the_answer() -> None:
        publisher.publish(OutboundText(scope=TEST_SCOPE, text="the answer"))
        delivered.set()

    run = threading.Thread(target=_publish_the_answer, name="redis-agent-event-publish", daemon=True)
    with caplog.at_level(logging.WARNING, logger="ouroboros.agent_events.redis_transport"):
        run.start()

        assert not delivered.wait(timeout=0.3), "a full transport must hold the run, not drop its answer"
        assert consumer.poll().text == "first"
        assert delivered.wait(timeout=5.0)

    assert consumer.poll().text == "the answer"
    assert any("waiting for room" in record.getMessage() for record in caplog.records), (
        "a tier that stopped draining must be legible from the producer's own logs"
    )
    run.join(timeout=1.0)


def test_no_append_this_module_issues_trims_by_age(local_client, monkeypatch) -> None:
    """`MAXLEN` evicts the oldest entry, which is the answer that has waited longest."""
    appended: list[dict] = []

    class _RecordingPipeline:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def xadd(self, *args, **kwargs):
            appended.append(kwargs)
            return self._inner.xadd(*args, **kwargs)

    inner_pipeline = local_client.pipeline
    monkeypatch.setattr(local_client, "pipeline", lambda *a, **k: _RecordingPipeline(inner_pipeline(*a, **k)))
    publisher, _consumer = _transport(local_client)

    publisher.publish(OutboundText(scope=TEST_SCOPE, text="the answer"))

    assert appended, "the publish must reach XADD"
    assert not [kwargs for kwargs in appended if kwargs.get("maxlen") or kwargs.get("minid")]


def test_an_entry_a_dying_consumer_never_deleted_does_not_shrink_the_cap(local_client, monkeypatch) -> None:
    """`XDEL` follows the read, so a crash between them leaks an undeliverable entry.

    Nothing else would ever remove it — `NOACK` left it in no pending list and it is behind
    the group's last-delivered id — while the publisher counts it in `XLEN` forever. A few
    supervisor restarts would lower the cap to nothing and wedge every runtime against a
    backlog no consumer can drain.
    """
    monkeypatch.setattr("ouroboros.agent_events.redis_transport._MAINTENANCE_INTERVAL_SEC", 0.0)
    stream = _fresh_stream()
    publisher, consumer = _transport(local_client, stream)
    publisher.publish(OutboundText(scope=TEST_SCOPE, text="m1"))
    publisher.publish(OutboundText(scope=TEST_SCOPE, text="m2"))

    def _dead_delete(*_args, **_kwargs):
        raise redis.exceptions.ConnectionError("the instance died between the read and the delete")

    monkeypatch.setattr(local_client, "xdel", _dead_delete)
    assert consumer.poll().text == "m1"  # taken, and its entry left behind
    monkeypatch.undo()
    monkeypatch.setattr("ouroboros.agent_events.redis_transport._MAINTENANCE_INTERVAL_SEC", 0.0)

    replacement = RedisAgentEventConsumer(client=local_client, stream=stream, group=f"{stream}:supervisor")
    assert replacement.poll().text == "m2"
    replacement.poll()  # the pass that trims what every group has been handed

    assert local_client.xlen(stream) == 0, "an entry nobody can receive must not count against the cap forever"


def test_a_backlog_no_reader_drains_refuses_the_publish_rather_than_holding_the_thread(
    local_client, monkeypatch
) -> None:
    """These publishes run on non-daemon worker threads that the interpreter joins at exit.

    An unbounded wait is therefore not back-pressure but a runtime that cannot drain, cannot
    exit and does not answer `SIGTERM`. At the bound the report is refused loudly.
    """
    monkeypatch.setattr("ouroboros.agent_events.redis_transport.MAX_PENDING_STREAM_EVENTS", 1)
    monkeypatch.setattr("ouroboros.agent_events.redis_transport.MAX_BACKPRESSURE_WAIT_SEC", 0.2)
    publisher, _consumer = _transport(local_client)
    publisher.publish(OutboundText(scope=TEST_SCOPE, text="the one that fills it"))

    started = time.monotonic()
    with pytest.raises(AgentEventBacklogFull, match="draining"):
        publisher.publish(OutboundText(scope=TEST_SCOPE, text="the answer nobody will read"))

    assert time.monotonic() - started < 5.0


def test_two_readers_in_one_group_take_every_report_exactly_once(local_client) -> None:
    """The deviation is about order, so delivery under competing readers must still hold.

    The double cannot prove how a real server races two clients on one entry — that is what
    the `integration` run is for — but it does prove neither reader drops or duplicates.
    """
    stream = _fresh_stream()
    group = f"{stream}:supervisor"
    publisher = RedisAgentEventPublisher(client=local_client, stream=stream)
    readers = [RedisAgentEventConsumer(client=local_client, stream=stream, group=group) for _ in range(2)]
    published = [f"m{index}" for index in range(6)]
    for text in published:
        publisher.publish(OutboundText(scope=TEST_SCOPE, text=text))

    delivered: list[str] = []
    while True:
        taken = [reader.poll(0.0) for reader in readers]
        delivered += [event.text for event in taken if event is not None]
        if not [event for event in taken if event is not None]:
            break

    assert sorted(delivered) == published, "one group, two readers: every report once, none lost"


def test_consumer_names_do_not_pile_up_in_the_group(local_client, monkeypatch) -> None:
    """Every instance mints a fresh name, so without a reap the group grows forever.

    `XGROUP DELCONSUMER` is refused on the message gateway because it destroys pending
    entries; under `NOACK` there are none, which is what makes it safe here.
    """
    monkeypatch.setattr("ouroboros.agent_events.redis_transport._MAINTENANCE_INTERVAL_SEC", 0.0)
    monkeypatch.setattr("ouroboros.agent_events.redis_transport._CONSUMER_IDLE_TTL_MS", 0)
    stream = _fresh_stream()
    group = f"{stream}:supervisor"
    for _ in range(5):  # five supervisor processes over the deployment's life
        RedisAgentEventConsumer(client=local_client, stream=stream, group=group).poll()

    survivor = RedisAgentEventConsumer(client=local_client, stream=stream, group=group)
    survivor.poll()

    names = [str(item["name"]) for item in local_client.xinfo_consumers(stream, group)]
    assert names == [survivor._consumer], "a departed instance's consumer name must not outlive it"


# -- the declared deviation -----------------------------------------------


def test_the_backend_partitions_by_scope_not_by_consumer(local_client) -> None:
    publisher, consumer = _transport(local_client)
    for event in (OutboundText(scope=TEST_SCOPE), TaskHeartbeat(scope=scope_with(TEST_SCOPE, owner_id="someone-else"))):
        assert publisher.partition_key(event) is event.scope
        assert consumer.partition_key(event) is event.scope


def test_a_blocking_poll_never_asks_for_longer_than_the_socket_will_wait() -> None:
    """redis-py raises on a blocking read that is doing exactly what it was asked to do."""
    from infra.redis_client import BROKER_SOCKET_TIMEOUT_SEC
    from ouroboros.agent_events import redis_transport

    assert redis_transport.MAX_POLL_BLOCK_SEC < BROKER_SOCKET_TIMEOUT_SEC


def test_an_entry_that_cannot_be_decoded_does_not_stall_the_drain(local_client, caplog) -> None:
    """Returning `None` would end the supervisor tick's drain with reports still queued."""
    stream = _fresh_stream()
    publisher, consumer = _transport(local_client, stream)
    assert consumer.poll() is None  # the group exists, so the bad entry is delivered to it
    local_client.xadd(stream, {"kind": "OutboundText", "payload": "{not json"})
    publisher.publish(OutboundText(scope=TEST_SCOPE, text="the one after it"))

    with caplog.at_level(logging.ERROR, logger="ouroboros.agent_events.redis_transport"):
        polled = consumer.poll()

    assert polled is not None
    assert polled.text == "the one after it"
    assert any("does not decode" in record.getMessage() for record in caplog.records)


def test_an_unknown_kind_is_recorded_and_skipped(local_client, caplog) -> None:
    """An older supervisor reading what a newer runtime published."""
    stream = _fresh_stream()
    publisher, consumer = _transport(local_client, stream)
    assert consumer.poll() is None
    local_client.xadd(stream, {"kind": "SomethingTheReaderHasNeverHeardOf", "payload": "{}"})
    publisher.publish(OutboundText(scope=TEST_SCOPE, text="the one after it"))

    with caplog.at_level(logging.WARNING, logger="ouroboros.agent_events.redis_transport"):
        polled = consumer.poll()

    assert polled is not None and polled.text == "the one after it"
    assert any("SomethingTheReaderHasNeverHeardOf" in record.getMessage() for record in caplog.records)


def test_an_entry_is_deleted_before_it_is_returned(local_client) -> None:
    """`XLEN` is the publisher's backlog measure, so a delivered entry may not sit in it."""
    stream = _fresh_stream()
    publisher, consumer = _transport(local_client, stream)
    publisher.publish(OutboundText(scope=TEST_SCOPE, text="the answer"))

    assert consumer.poll() is not None
    assert local_client.xlen(stream) == 0


def test_a_report_published_before_any_supervisor_existed_is_still_delivered(local_client) -> None:
    """The group starts at `0`: starting at the present would strand published answers."""
    stream = _fresh_stream()
    publisher = RedisAgentEventPublisher(client=local_client, stream=stream)
    publisher.publish(OutboundText(scope=TEST_SCOPE, text="published before the supervisor started"))

    consumer = RedisAgentEventConsumer(client=local_client, stream=stream, group=f"{stream}:supervisor")

    assert consumer.poll().text == "published before the supervisor started"


def test_a_waiting_poll_returns_nothing_after_its_timeout(local_client) -> None:
    _publisher, consumer = _transport(local_client)

    started = time.monotonic()
    assert consumer.poll(timeout=0.2) is None
    assert 0.15 <= time.monotonic() - started < 2.0


# -- what must be refused before anything is written ----------------------


class _RefusingClient:
    """Every Redis command is a failure: the report must never reach one."""

    def __getattr__(self, name: str):
        def _refuse(*args, **kwargs):
            raise AssertionError(f"the report must be refused before {name} is issued")

        return _refuse


def test_the_publisher_refuses_anything_that_is_not_a_report() -> None:
    publisher = RedisAgentEventPublisher(client=_RefusingClient(), stream="s:agent-events")

    with pytest.raises(TypeError):
        publisher.publish({"scope": TEST_SCOPE, "text": "hello"})  # type: ignore[arg-type]


def test_a_value_no_wire_format_carries_is_refused_before_the_append() -> None:
    """Refused where the call site still names the producer."""
    import pathlib

    publisher = RedisAgentEventPublisher(client=_RefusingClient(), stream="s:agent-events")
    unsafe = OutboundText(scope=TEST_SCOPE, text="hi", transport={"hint": pathlib.Path("/tmp/x")})

    with pytest.raises(TypeError):
        publisher.publish(unsafe)


def test_both_halves_require_the_keys_they_work_on() -> None:
    with pytest.raises(ValueError, match="stream"):
        RedisAgentEventPublisher(client=_RefusingClient(), stream="")
    with pytest.raises(ValueError, match="group"):
        RedisAgentEventConsumer(client=_RefusingClient(), stream="s:agent-events", group="")


# -- what a poll does when the broker is not there --------------------------


class _UnreachableClient:
    """A client whose every command fails, as one pointed at a Redis that is restarting."""

    def __init__(self) -> None:
        self.attempts = 0

    def __getattr__(self, name: str):
        self.attempts += 1

        def _fail(*_args, **_kwargs):
            raise redis.exceptions.ConnectionError("the broker is not there")

        return _fail


def test_a_broker_that_is_not_there_returns_nothing_rather_than_crashing_the_tick(caplog) -> None:
    """A missing broker pauses the tick instead of escaping from the poll."""
    unreachable = _UnreachableClient()
    consumer = RedisAgentEventConsumer(client=unreachable, stream="s:agent-events", group="s:agent-events:supervisor")

    with caplog.at_level(logging.WARNING, logger="ouroboros.agent_events.redis_transport"):
        started = time.monotonic()
        for _ in range(50):  # what the tick would do while Redis restarts
            assert consumer.poll(0.01) is None
        elapsed = time.monotonic() - started

    assert elapsed >= 0.4, "each poll must spend the timeout the tick meant to spend"
    assert unreachable.attempts <= 2, "a broker known to be down must not be dialled once per poll"
    assert len(caplog.records) == 1, "one outage is one report, not one per attempt"


def test_a_broker_that_comes_back_is_drained_without_a_restart(local_client, monkeypatch, caplog) -> None:
    """The outage is a delay, not a latch: the tier must recover on its own, and say so.

    Nothing is lost by the pause either — unlike the memory seam, this stream keeps what
    nobody has taken, so the reports published before the outage are still there. The
    recovery line is what tells an operator that, and what resets the report gate so the next
    failure is reported with its own cause rather than inheriting this one's silence.
    """
    monkeypatch.setattr("ouroboros.agent_events.redis_transport.BROKER_RETRY_SEC", 0.05)
    stream = _fresh_stream()
    publisher, consumer = _transport(local_client, stream)
    publisher.publish(OutboundText(scope=TEST_SCOPE, text="published while the broker was up"))
    down = {"yes": True}
    inner_read = local_client.xreadgroup

    def _sometimes(*args, **kwargs):
        if down["yes"]:
            raise redis.exceptions.ConnectionError("the broker is not there yet")
        return inner_read(*args, **kwargs)

    local_client.xreadgroup = _sometimes  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="ouroboros.agent_events.redis_transport"):
        assert consumer.poll(0.01) is None

        down["yes"] = False
        time.sleep(0.06)

        polled = consumer.poll(1.0)

    assert polled is not None and polled.text == "published while the broker was up"
    assert [record.getMessage() for record in caplog.records][-1].endswith("can be read again")


def test_a_backlog_of_unreadable_entries_returns_to_the_tick_rather_than_owning_the_thread(local_client) -> None:
    """The skip is a `continue` past the deadline check, so it needs a bound of its own."""
    stream = _fresh_stream()
    publisher, consumer = _transport(local_client, stream)
    assert consumer.poll() is None  # the group exists, so what follows is delivered to it
    for _ in range(MAX_SKIPPED_ENTRIES_PER_POLL + 4):
        local_client.xadd(stream, {"kind": "OutboundText", "payload": "{not json"})
    publisher.publish(OutboundText(scope=TEST_SCOPE, text="the one after them"))

    assert consumer.poll(0.0) is None, "one poll may not drain an unbounded backlog of them"

    assert consumer.poll(0.0).text == "the one after them"
