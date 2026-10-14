"""The Redis Streams backend, held to the seam's conformance suite.

The suite is the acceptance for this backend: every semantic that only matters for a
broker — offsets, redelivery, `SHARED` versus `BROADCAST`, a poll that actually blocks — is
otherwise untested by construction, and this is the first backend that can answer the
redelivery case at all, because it is the first whose queue outlives its consumer.

It runs twice over the same backend code. Against `fakeredis`, a local double, it is part
of the default gate, so the backend is not a module CI never executes. Against a real
Redis 7 it is the acceptance: point `OUROBOROS_TEST_REDIS_URL` at one and run
``uv run pytest tests/transport/test_transport_redis.py -m integration``.

The double is `tests/helpers/redis_double.py`, shared with the agent-event transport's
suite, and its docstring says what a green default gate does not prove.
"""

from __future__ import annotations

import logging
import os
import pathlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from ouroboros.contracts.stream_replies import first_entry, row as _row, text as _text
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.redis_double import async_client_onto, client_onto, fake_server
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.transport_conformance import (
    POLL_TIMEOUT_SEC,
    InboxConformance,
    OutboxConformance,
    envelope,
    full_envelope,
    full_message,
    message,
)
from transport.infra.redis_gateway import (
    MAX_SKIPPED_ENTRIES_PER_POLL,
    RedisInbox,
    RedisMessageGateway,
    RedisOutbox,
    _slice_ms,
)
from transport.interfaces import IInbox, IMessageGateway, IOutbox, SubscriptionMode

TEST_REDIS_URL = os.environ.get("OUROBOROS_TEST_REDIS_URL", "")

needs_redis = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_REDIS_URL,
        reason="set OUROBOROS_TEST_REDIS_URL to a Redis 7 to run the gateway conformance suite",
    ),
]

#: Key prefixes this test minted, deleted afterwards. A `SHARED` group is never destroyed
#: by design, so one shared stream would carry a case's groups, its released-consumer
#: entries and its mode claims into the next case.
_MINTED: list[str] = []


def _client():
    import redis

    return redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True, socket_connect_timeout=5.0, socket_timeout=10.0)


class _RealServer:
    """Clients onto the configured test Redis."""

    def client(self):
        return _client()


class _LocalDouble:
    """Clients onto one `FakeServer` per case, so two of them share a keyspace."""

    def client(self):
        if not hasattr(self, "_server"):
            self._server = fake_server()
        return client_onto(self._server)


def _broken_client():
    """A client whose every command fails: nothing listens on port 1."""
    import redis

    return redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, decode_responses=True)


def _outbox(client, stream: str) -> RedisOutbox:
    """One client in both roles: the split exists for the retry policy, not for these cases."""
    return RedisOutbox(client=client, append_client=client, stream=stream)


def _inbox(client, stream: str, *, group: str = "supervisor", consumer: str = "supervisor-1") -> RedisInbox:
    return RedisInbox(client=client, append_client=client, stream=stream, group=group, consumer=consumer)


def _fresh_prefix() -> str:
    prefix = f"ouroboros-test:{uuid.uuid4().hex}"
    _MINTED.append(prefix)
    return prefix


@pytest.fixture(autouse=True)
def _drop_minted_keys():
    yield
    if not TEST_REDIS_URL or not _MINTED:
        _MINTED.clear()
        return
    client = _client()
    for prefix in _MINTED:
        keys = client.keys(f"{prefix}*")
        if keys:
            client.delete(*keys)
    _MINTED.clear()


class _RedisOutboxConformance(OutboxConformance):
    """The suite unchanged; only where the server lives varies between the subclasses."""

    def make_outbox(self) -> RedisOutbox:
        client = self.client()
        return _outbox(client, f"{_fresh_prefix()}:outbound")

    def supports_shared_subscriptions(self) -> bool:
        return True

    @parametrize_from_dict({"live": {"closed": False}, "closed": {"closed": True}})
    def test_deferred_order_is_shared_between_outbox_instances(self, closed):
        stream = f"{_fresh_prefix()}:outbound"
        producer = _outbox(self.client(), stream)
        peer = _outbox(self.client(), stream)
        first = producer.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        second = peer.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        try:
            producer.publish(message("photo", scope=TEST_SCOPE))
            producer.publish(message("following", scope=TEST_SCOPE))
            producer.publish(message("other", scope=TEST_SCOPE.model_copy(update={"owner_id": "other"})))
            waiting = first.poll(POLL_TIMEOUT_SEC)
            first.defer(waiting, delay=1.0)
            if closed:
                first.close()
            other = second.poll(0)
            assert other is not None and other.message.text == "other"
            second.ack(other)
            assert second.poll(0) is None
            holder = second if closed else first
            resumed = holder.poll(2.0)
            assert resumed is not None and resumed.message.text == "photo"
            holder.ack(resumed)
            following = second.poll(POLL_TIMEOUT_SEC)
            assert following is not None and following.message.text == "following"
            second.ack(following)
        finally:
            first.close()
            second.close()

    def test_competing_readers_enroll_the_entry_they_actually_take(self, monkeypatch):
        stream = f"{_fresh_prefix()}:outbound"
        client = self.client()
        producer = _outbox(client, stream)
        slow = producer.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        peer = _outbox(self.client(), stream).reader(name="dialogs", mode=SubscriptionMode.SHARED)
        paused, resume = threading.Event(), threading.Event()
        original_pipeline = client.pipeline
        armed = True

        def pipeline(*args, **kwargs):
            pending = original_pipeline(*args, **kwargs)
            execute = pending.execute

            def pause_once(*args, **kwargs):
                nonlocal armed
                if armed and pending.watching:
                    armed = False
                    paused.set()
                    assert resume.wait(POLL_TIMEOUT_SEC * 8)
                return execute(*args, **kwargs)

            monkeypatch.setattr(pending, "execute", pause_once)
            return pending

        monkeypatch.setattr(client, "pipeline", pipeline)
        try:
            producer.publish(message("first"))
            producer.publish(message("second"))
            with ThreadPoolExecutor(max_workers=1) as workers:
                reading = workers.submit(slow.poll, POLL_TIMEOUT_SEC)
                try:
                    assert paused.wait(POLL_TIMEOUT_SEC * 4)
                    first = peer.poll(POLL_TIMEOUT_SEC)
                    assert first is not None and first.message.text == "first"
                    peer.ack(first)
                finally:
                    resume.set()
                second = reading.result(timeout=POLL_TIMEOUT_SEC * 8)
                assert second is not None and second.message.text == "second"
                slow.ack(second)
        finally:
            resume.set()
            slow.close()
            peer.close()

    def test_crash_replay_scans_past_live_deferred_owners(self, monkeypatch):
        monkeypatch.setattr("transport.infra.redis_gateway._MAINTENANCE_INTERVAL_SEC", 0.0)
        client = self.client()
        stream = f"{_fresh_prefix()}:outbound"
        producer = _outbox(client, stream)
        live = producer.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        crashed = producer.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        try:
            for index in range(12):
                owner = TEST_SCOPE.model_copy(update={"owner_id": f"live-{index}"})
                producer.publish(message(f"live-{index}", scope=owner))
                delivered = live.poll(POLL_TIMEOUT_SEC)
                assert delivered is not None
                live.defer(delivered, delay=60)
            producer.publish(message("from crashed reader", scope=TEST_SCOPE))
            lost = crashed.poll(POLL_TIMEOUT_SEC)
            assert lost is not None and lost.message.text == "from crashed reader"
            entry_id = client.xrevrange(stream, count=1)[0][0]
            pending = client.xpending_range(stream, "dialogs", min=entry_id, max=entry_id, count=1)[0]
            client.xclaim(stream, "dialogs", pending["consumer"], 0, [entry_id], idle=600_000)

            replayed = None
            for _ in range(3):
                replayed = live.poll(0)
                if replayed is not None:
                    break
            assert replayed is not None and replayed.message.text == "from crashed reader"
            live.ack(replayed)
        finally:
            live.close()
            crashed.close()

    def test_deferred_metadata_stays_bounded_when_stream_entries_are_trimmed(self, monkeypatch):
        monkeypatch.setattr("transport.infra.redis_gateway._MAX_STREAM_ENTRIES", 2)
        client = self.client()
        stream = f"{_fresh_prefix()}:outbound"
        producer = _outbox(client, stream)
        reader = producer.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        try:
            for index in range(12):
                owner = TEST_SCOPE.model_copy(update={"owner_id": f"owner-{index}"})
                producer.publish(message(str(index), scope=owner))
                delivered = reader.poll(POLL_TIMEOUT_SEC)
                assert delivered is not None and delivered.message.text == str(index)
                reader.defer(delivered, delay=60)
                assert client.xlen(stream) <= 2
                assert client.zcard(f"{stream}:waiting:dialogs") <= 2
                assert client.hlen(f"{stream}:attempts:dialogs") <= 2
                assert client.hlen(f"{stream}:retry:dialogs") <= 3
        finally:
            reader.close()

    def break_publish(self, outbox: RedisOutbox) -> bool:
        """Break the producer path alone.

        The append has a client of its own precisely because its retry policy differs, which
        is also what lets this break the producer and nothing else: the readers were opened
        before this runs and must keep polling.
        """
        outbox._append_client = _broken_client()
        return True

    def shrink_retention(self, outbox: RedisOutbox, monkeypatch) -> int:
        """The bound is a module constant here, so the case can reach it cheaply."""
        del outbox
        monkeypatch.setattr("transport.infra.redis_gateway._MAX_STREAM_ENTRIES", 3)
        return 3


class _RedisInboxConformance(InboxConformance):
    def make_inbox(self) -> RedisInbox:
        client = self.client()
        self._built = {
            "client": client,
            "append_client": client,
            "stream": f"{_fresh_prefix()}:inbound",
            "group": "supervisor",
            "consumer": "instance-1",
        }
        return RedisInbox(**self._built)

    def replacement_consumer(self, inbox: RedisInbox) -> RedisInbox:
        """A restarted instance: same group *and* the same consumer name.

        The first backend that can answer this case — its queue outlives the consumer that
        stopped, so there is somebody to redeliver to.
        """
        del inbox
        return RedisInbox(**self._built)


class TestLocalDoubleOutbox(_LocalDouble, _RedisOutboxConformance):
    pass


class TestLocalDoubleInbox(_LocalDouble, _RedisInboxConformance):
    pass


class TestRedisOutbox(_RealServer, _RedisOutboxConformance):
    pytestmark = needs_redis


class TestRedisInbox(_RealServer, _RedisInboxConformance):
    pytestmark = needs_redis


# -- what must be refused before anything is written -----------------------


class _RefusingClient:
    """Every Redis command is a failure: the payload must never reach one."""

    def __getattr__(self, name: str):
        def _refuse(*args, **kwargs):
            raise AssertionError(f"the payload must be refused before {name} is issued")

        return _refuse


def _offline_inbox() -> RedisInbox:
    return _inbox(_RefusingClient(), "s:inbound", consumer="c")


def _offline_outbox() -> RedisOutbox:
    return _outbox(_RefusingClient(), "s:outbound")


def test_the_inbox_refuses_anything_that_is_not_an_envelope_before_the_append() -> None:
    with pytest.raises(TypeError):
        _offline_inbox().submit({"chat_id": 1, "text": "hello"})


def test_the_outbox_refuses_anything_that_is_not_an_outbound_message_before_the_append() -> None:
    with pytest.raises(TypeError):
        _offline_outbox().publish({"chat_id": 1, "text": "hello"})


def test_a_value_no_wire_format_carries_is_refused_before_the_append() -> None:
    """`ensure_wire_safe` runs where the call site still names the producer."""
    unsafe_envelope = full_envelope()
    unsafe_envelope.transport["hint"] = pathlib.Path("/tmp/x")
    with pytest.raises(TypeError):
        _offline_inbox().submit(unsafe_envelope)

    unsafe_message = full_message()
    unsafe_message.transport["hint"] = pathlib.Path("/tmp/x")
    with pytest.raises(TypeError):
        _offline_outbox().publish(unsafe_message)


def test_a_reader_requires_the_transport_name() -> None:
    with pytest.raises(ValueError, match="transport's name"):
        _offline_outbox().reader(name="", mode=SubscriptionMode.BROADCAST)


def test_a_reader_name_cannot_forge_a_group_shape() -> None:
    """`#` separates the mode marker from the name, so a name may not contain one."""
    with pytest.raises(ValueError, match="#"):
        _offline_outbox().reader(name="browser#b#forged", mode=SubscriptionMode.SHARED)


def test_the_inbox_requires_a_consumer_name_of_its_own() -> None:
    """Two instances sharing one read each other's in-flight entry as a redelivery."""
    with pytest.raises(ValueError, match="consumer"):
        _inbox(_RefusingClient(), "s:inbound", consumer="")


def test_a_blocking_read_is_sliced_and_never_waits_out_the_whole_timeout_in_one_command() -> None:
    """`close` cannot interrupt a command already in flight, so no slice may be long."""
    deadline = time.monotonic() + 30.0

    assert 0 < _slice_ms(deadline) <= 250
    assert _slice_ms(time.monotonic() - 1) == 0, "a lapsed deadline must not block at all"
    assert _slice_ms(time.monotonic() + 0.05) <= 250


def test_a_pending_entry_whose_stream_entry_is_gone_is_skipped() -> None:
    """Redis returns `(id, None)` for one, and reading it as fields would crash the reader."""
    assert first_entry([["s", [("1-0", None)]]]) is None
    assert first_entry([["s", [("1-0", None), ("2-0", {"payload": "x"})]]]) == ("2-0", {"payload": "x"})
    assert first_entry({"s": [("1-0", {"payload": "x"})]}) == ("1-0", {"payload": "x"})


def test_the_gateway_implements_the_seam() -> None:
    gateway = RedisMessageGateway(
        inbox=_offline_inbox(), outbox=_offline_outbox(), async_client=async_client_onto(fake_server())
    )

    assert isinstance(gateway, IMessageGateway)
    assert isinstance(gateway.inbox, IInbox)
    assert isinstance(gateway.outbox, IOutbox)


def test_both_directions_must_be_passed_explicitly() -> None:
    with pytest.raises(TypeError):
        RedisMessageGateway()


# -- the parts of the design a black-box suite does not reach --------------


@pytest.fixture(
    params=[
        pytest.param("double", id="double"),
        pytest.param("server", id="server", marks=needs_redis),
    ]
)
def clients(request):
    """A factory of clients onto one keyspace: the local double, and a real Redis.

    Both, because these cases are about semantics the server owns — the group list, what
    `XINFO` reports after a trim — as much as about this backend's use of them.
    """
    if request.param == "server":
        return _client

    import fakeredis

    server = fakeredis.FakeServer()
    return lambda: fakeredis.FakeStrictRedis(decode_responses=True, server=server)


def test_a_second_instance_cannot_open_one_name_in_the_other_mode(clients) -> None:
    """The claim is the set of groups on the stream, so every instance reads it."""
    stream = f"{_fresh_prefix()}:outbound"
    first = _outbox(clients(), stream)
    second = _outbox(clients(), stream)
    held = first.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        with pytest.raises(ValueError, match="broadcast"):
            second.reader(name="browser", mode=SubscriptionMode.SHARED)
    finally:
        held.close()


def test_a_shared_claim_persists_with_the_progress_it_names(clients) -> None:
    """Discarding the retained subscription would lose the messages the mode keeps."""
    stream = f"{_fresh_prefix()}:outbound"
    first = _outbox(clients(), stream)
    first.reader(name="dialogs", mode=SubscriptionMode.SHARED).close()

    second = _outbox(clients(), stream)
    with pytest.raises(ValueError, match="shared"):
        second.reader(name="dialogs", mode=SubscriptionMode.BROADCAST)


def test_a_broadcast_group_whose_process_did_not_return_is_swept(clients, monkeypatch) -> None:
    """`close` is the orderly release and cannot be the only one."""
    monkeypatch.setattr("transport.infra.redis_gateway._MAINTENANCE_INTERVAL_SEC", 0.0)
    client = clients()
    stream = f"{_fresh_prefix()}:outbound"
    outbox = _outbox(client, stream)
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    group = reader._group  # the heartbeat a live reader refreshes is keyed on it

    client.delete(f"{stream}:hb:{group}")  # the process stopped refreshing it
    outbox.publish(message("the one that sweeps"))

    assert [item["name"] for item in client.xinfo_groups(stream)] == []
    # And the claim lapsed with the last group: the name may be opened in either mode.
    outbox.reader(name="browser", mode=SubscriptionMode.SHARED).close()


def test_a_reader_that_receives_nothing_still_says_it_is_alive(clients, monkeypatch) -> None:
    """An idle broadcast reader is live, and `poll` is the only call it makes.

    A heartbeat refreshed only when a message arrives would have the sweep take the group
    of a reader whose owner simply said nothing for a TTL.
    """
    monkeypatch.setattr("transport.infra.redis_gateway._MAINTENANCE_INTERVAL_SEC", 0.0)
    client = clients()
    stream = f"{_fresh_prefix()}:outbound"
    outbox = _outbox(client, stream)
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        client.delete(f"{stream}:hb:{reader._group}")  # as if the TTL had lapsed
        assert reader.poll(0.0) is None

        outbox.publish(message("after the sweep had its turn"))
        delivered = reader.poll(POLL_TIMEOUT_SEC)

        assert delivered is not None
        assert delivered.message.text == "after the sweep had its turn"
        reader.ack(delivered)
    finally:
        reader.close()


def test_the_first_publish_onto_a_fresh_stream_is_quiet(clients, monkeypatch, caplog) -> None:
    """Housekeeping runs before the first append and must not report the absence it finds."""
    monkeypatch.setattr("transport.infra.redis_gateway._MAINTENANCE_INTERVAL_SEC", 0.0)
    outbox = _outbox(clients(), f"{_fresh_prefix()}:outbound")

    with caplog.at_level(logging.WARNING, logger="transport.infra.redis_gateway"):
        outbox.publish(message("the first one"))

    assert caplog.records == []


def test_a_broadcast_group_whose_heartbeat_is_present_is_left_alone(clients, monkeypatch) -> None:
    """The failure path of the same mechanism: the sweep runs and takes nothing."""
    monkeypatch.setattr("transport.infra.redis_gateway._MAINTENANCE_INTERVAL_SEC", 0.0)
    client = clients()
    stream = f"{_fresh_prefix()}:outbound"
    outbox = _outbox(client, stream)
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        outbox.publish(message("for a live socket"))  # the publish that sweeps
        delivered = reader.poll(POLL_TIMEOUT_SEC)

        assert [item["name"] for item in client.xinfo_groups(stream)] == [reader._group]
        assert delivered is not None
        assert delivered.message.text == "for a live socket"
        reader.ack(delivered)
    finally:
        reader.close()


def test_settling_a_delivery_refreshes_the_heartbeat_too(clients, monkeypatch) -> None:
    """`ack` and `nack` are calls a live reader makes, so they are liveness as much as `poll`."""
    monkeypatch.setattr("transport.infra.redis_gateway._HEARTBEAT_REFRESH_SEC", 0.0)
    client = clients()
    stream = f"{_fresh_prefix()}:outbound"
    outbox = _outbox(client, stream)
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    heartbeat = f"{stream}:hb:{reader._group}"
    try:
        outbox.publish(message("one"))
        delivered = reader.poll(POLL_TIMEOUT_SEC)
        assert delivered is not None

        client.delete(heartbeat)
        reader.nack(delivered)
        assert client.exists(heartbeat), "nack is a call a live reader makes"

        again = reader.poll(POLL_TIMEOUT_SEC)
        assert again is not None
        client.delete(heartbeat)
        reader.ack(again)
        assert client.exists(heartbeat), "and so is ack"
    finally:
        reader.close()


def test_a_refresh_restores_the_whole_ttl_rather_than_topping_it_up(clients, monkeypatch) -> None:
    """What keeps a live reader alive is the gap between refreshes against the whole window.

    A refresh that only extended a nearly-lapsed key, or that set a shorter window than the
    one the sweep waits out, would let the sweep take the group of a reader that is polling.
    """
    from transport.infra import redis_gateway

    monkeypatch.setattr("transport.infra.redis_gateway._HEARTBEAT_REFRESH_SEC", 0.0)
    client = clients()
    stream = f"{_fresh_prefix()}:outbound"
    reader = _outbox(client, stream).reader(name="browser", mode=SubscriptionMode.BROADCAST)
    heartbeat = f"{stream}:hb:{reader._group}"
    try:
        client.expire(heartbeat, 2)  # nearly lapsed
        assert reader.poll(0.0) is None

        assert client.ttl(heartbeat) > redis_gateway._BROADCAST_HEARTBEAT_TTL_SEC / 2
    finally:
        reader.close()


def test_closing_a_shared_reader_does_not_take_back_what_a_peer_now_holds(clients, monkeypatch) -> None:
    """The crash backstop may hand a slow reader's entry to a peer before it closes.

    Claiming it back would put a message another reader is delivering right now into the pool
    a third one takes it from, and the user would see the same answer twice more.
    """
    monkeypatch.setattr("transport.infra.redis_gateway._OUTBOX_RECLAIM_MIN_IDLE_MS", 0)
    outbox = _outbox(clients(), f"{_fresh_prefix()}:outbound")
    slow = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
    peer = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
    third = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
    try:
        outbox.publish(message("only", source="dialogs"))
        assert slow.poll(POLL_TIMEOUT_SEC) is not None
        stolen = peer.poll(POLL_TIMEOUT_SEC)  # the backstop hands it over, unacknowledged
        assert stolen is not None

        # Nobody may steal from here on, so what the third reader sees can only come from
        # the released pool the closing reader writes to.
        monkeypatch.setattr("transport.infra.redis_gateway._OUTBOX_RECLAIM_MIN_IDLE_MS", 30_000)
        slow.close()  # the slow reader returns at last, holding a receipt for somebody else's entry

        assert third.poll(POLL_TIMEOUT_SEC) is None, "an entry a peer is delivering must not be released"
    finally:
        peer.close()
        third.close()


def test_closing_a_shared_reader_does_not_discard_what_it_negatively_acknowledged(clients) -> None:
    """A `nack`ed entry sits in the pending list with no in-flight receipt beside it.

    Releasing only the tracked delivery would leave it there, and removing the consumer would
    then take it with him — a message that reached nobody and that nothing can redeliver.
    """
    outbox = _outbox(clients(), f"{_fresh_prefix()}:outbound")
    failing = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
    peer = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
    try:
        outbox.publish(message("delivery failed", source="dialogs"))
        delivered = failing.poll(POLL_TIMEOUT_SEC)
        assert delivered is not None
        failing.nack(delivered)  # the transport gives up and the instance goes away
        failing.close()

        resumed = peer.poll(POLL_TIMEOUT_SEC)

        assert resumed is not None
        assert resumed.message.text == "delivery failed"
        peer.ack(resumed)
    finally:
        peer.close()


def test_the_drop_report_names_the_group_furthest_behind(clients, monkeypatch, caplog) -> None:
    """Drops are the operator's only signal, and the actionable fact is who stopped.

    Two groups, one of them keeping up, so naming the wrong one is a failure rather than a
    coin toss: the report must point at the consumer that stopped consuming.
    """
    monkeypatch.setattr("transport.infra.redis_gateway._MAINTENANCE_INTERVAL_SEC", 0.0)
    monkeypatch.setattr("transport.infra.redis_gateway._DROP_REPORT_INTERVAL_SEC", 0.0)
    monkeypatch.setattr("transport.infra.redis_gateway._MAX_STREAM_ENTRIES", 2)
    outbox = _outbox(clients(), f"{_fresh_prefix()}:outbound")
    stalled = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
    keeping_up = outbox.reader(name="a2a", mode=SubscriptionMode.SHARED)
    try:
        with caplog.at_level(logging.ERROR, logger="transport.infra.redis_gateway"):
            for index in range(6):
                assert outbox.publish(message(f"m{index}", source="dialogs")) is None
                delivered = keeping_up.poll(0.0)
                if delivered is not None:
                    keeping_up.ack(delivered)

        reports = [record for record in caplog.records if "lost" in record.getMessage()]
        assert reports, "a bound that drops must say so"
        assert "'dialogs'" in reports[-1].getMessage()
        assert "'a2a'" not in reports[-1].getMessage(), "the report must name the one that stopped"
    finally:
        stalled.close()
        keeping_up.close()


def test_a_reader_whose_group_vanished_resumes_instead_of_dying(clients, monkeypatch) -> None:
    """A swept group under a live reader used to end delivery for the life of the process.

    `run_reader_loop` stops consuming on a raise and its caller closes the reader without
    reopening it, while the readiness probe keeps answering 200.
    """
    monkeypatch.setattr("transport.infra.redis_gateway._MAINTENANCE_INTERVAL_SEC", 0.0)
    client = clients()
    stream = f"{_fresh_prefix()}:outbound"
    outbox = _outbox(client, stream)
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        client.delete(f"{stream}:hb:{reader._group}")
        outbox.publish(message("swept away with the group"))  # the publish that sweeps it
        assert reader.poll(0.0) is None

        outbox.publish(message("after the group came back"))
        delivered = reader.poll(POLL_TIMEOUT_SEC)

        assert delivered is not None
        assert delivered.message.text == "after the group came back"
        reader.ack(delivered)
    finally:
        reader.close()


def test_a_failing_sweep_does_not_silence_the_drop_report(clients, monkeypatch, caplog) -> None:
    """They shared one guard, so a sweep that always fails hid the only actionable signal."""
    monkeypatch.setattr("transport.infra.redis_gateway._MAINTENANCE_INTERVAL_SEC", 0.0)
    monkeypatch.setattr("transport.infra.redis_gateway._MAX_STREAM_ENTRIES", 2)

    outbox = _outbox(clients(), f"{_fresh_prefix()}:outbound")
    stalled = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)

    def _refuse(_self, _execute=None) -> None:
        raise RuntimeError("this deployment's ACL has no XGROUP")

    monkeypatch.setattr("transport.infra.redis_gateway.RedisOutbox._sweep_orphaned_broadcast", _refuse)
    try:
        with caplog.at_level(logging.WARNING, logger="transport.infra.redis_gateway"):
            for index in range(6):
                assert outbox.publish(message(f"m{index}", source="dialogs")) is None

        assert any("lost messages" in record.getMessage() for record in caplog.records)
    finally:
        stalled.close()


def test_a_server_that_cannot_report_drops_says_so(clients, monkeypatch, caplog) -> None:
    """`entries-added` arrived in Redis 7.0; its absence used to read as "nothing was trimmed"."""
    monkeypatch.setattr("transport.infra.redis_gateway._MAINTENANCE_INTERVAL_SEC", 0.0)
    client = clients()
    outbox = _outbox(client, f"{_fresh_prefix()}:outbound")
    inner_info = client.response_callbacks["XINFO STREAM"]
    monkeypatch.setitem(
        client.response_callbacks,
        "XINFO STREAM",
        lambda *a, **k: {k: v for k, v in inner_info(*a, **k).items() if k != "entries-added"},
    )
    reader = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
    try:
        with caplog.at_level(logging.ERROR, logger="transport.infra.redis_gateway"):
            outbox.publish(message("one", source="dialogs"))
            outbox.publish(message("two", source="dialogs"))

        assert any("Redis 7.0" in record.getMessage() for record in caplog.records)
    finally:
        reader.close()


def test_the_acknowledgement_lands_before_the_reader_is_free_to_poll(clients) -> None:
    """Freeing the reader first lets the woken poll read the same entry out of the pending list.

    A blocked `poll` waits on the very condition `ack` signals, so it runs the instant the
    slot is cleared. If the `XACK` has not landed by then the entry is still pending for this
    consumer, the poll's first read returns it, and the message is delivered a second time
    under a second receipt with nothing to tell the two apart. The `XACK` is made slow here
    so the window is a fact rather than a coin toss.
    """
    import threading

    class _SlowAck:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def xack(self, *args, **kwargs):
            time.sleep(0.3)
            return self._inner.xack(*args, **kwargs)

    outbox = _outbox(_SlowAck(clients()), f"{_fresh_prefix()}:outbound")
    reader = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
    second: list[object] = []
    try:
        outbox.publish(message("once", source="dialogs"))
        delivered = reader.poll(POLL_TIMEOUT_SEC)
        assert delivered is not None

        waiting = threading.Thread(target=lambda: second.append(reader.poll(POLL_TIMEOUT_SEC * 4)), daemon=True)
        waiting.start()
        time.sleep(0.1)  # long enough for it to reach the stall gate and wait there
        reader.ack(delivered)
        waiting.join(timeout=POLL_TIMEOUT_SEC * 8)

        assert second == [None], "an acknowledged entry must not be handed to a waiting poll"
    finally:
        reader.close()


def test_the_drop_report_is_rate_limited_rather_than_one_line_per_publish(clients, monkeypatch, caplog) -> None:
    """The condition persists until somebody drains the group; the report must not."""
    monkeypatch.setattr("transport.infra.redis_gateway._MAINTENANCE_INTERVAL_SEC", 0.0)
    monkeypatch.setattr("transport.infra.redis_gateway._MAX_STREAM_ENTRIES", 2)
    outbox = _outbox(clients(), f"{_fresh_prefix()}:outbound")
    stalled = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
    try:
        with caplog.at_level(logging.ERROR, logger="transport.infra.redis_gateway"):
            for index in range(12):
                assert outbox.publish(message(f"m{index}", source="dialogs")) is None

        reports = [record for record in caplog.records if "lost" in record.getMessage()]
        assert len(reports) == 1, "a report per publish is not a diagnostic"
    finally:
        stalled.close()


def test_an_entry_that_cannot_be_decoded_is_acknowledged_and_dropped(clients, caplog) -> None:
    """Redelivering it would stall the subscription forever."""
    client = clients()
    stream = f"{_fresh_prefix()}:outbound"
    outbox = _outbox(client, stream)
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        with caplog.at_level(logging.ERROR, logger="transport.infra.redis_gateway"):
            client.xadd(stream, {"payload": "{not json"})
            outbox.publish(message("the one after it"))

            delivered = reader.poll(POLL_TIMEOUT_SEC)

        assert delivered is not None
        assert delivered.message.text == "the one after it"
        assert any("does not decode" in record.getMessage() for record in caplog.records)
        reader.ack(delivered)
    finally:
        reader.close()


def test_a_submitted_envelope_is_never_dropped_by_a_retention_bound(clients, monkeypatch) -> None:
    """The outbox bounds what it retains; the inbox must not.

    `submit` returning means the message was accepted for delivery, and the transport marks
    its journal row processed on the strength of that — a dropped envelope is a user message
    nothing will ever replay. A supervisor tier that is down is exactly when the backlog is
    long and exactly when losing it is worst.
    """
    monkeypatch.setattr("transport.infra.redis_gateway._MAX_STREAM_ENTRIES", 2)
    stream = f"{_fresh_prefix()}:inbound"
    inbox = _inbox(clients(), stream)
    for index in range(5):
        inbox.submit(envelope(f"m{index}"))

    polled = [inbox.poll(POLL_TIMEOUT_SEC) for _ in range(5)]

    assert [item.text for item in polled if item is not None] == [f"m{index}" for index in range(5)]


def test_a_drained_inbound_stream_does_not_grow_forever(clients, monkeypatch) -> None:
    """`XACK` clears a pending list and leaves the entry in the stream.

    Without a trim a *healthy* deployment grows the inbound key without limit, and Redis's own
    `maxmemory` policy then either fails every `submit` or evicts the whole key.
    """
    monkeypatch.setattr("transport.infra.redis_gateway._MAINTENANCE_INTERVAL_SEC", 0.0)
    client = clients()
    stream = f"{_fresh_prefix()}:inbound"
    inbox = _inbox(client, stream)
    for index in range(5):
        inbox.submit(envelope(f"m{index}"))
    for _ in range(5):
        assert inbox.poll(POLL_TIMEOUT_SEC) is not None
    inbox.poll(0.0)  # the poll that settles the last one and trims what is settled

    assert client.xlen(stream) == 0


def test_an_unread_backlog_is_never_trimmed(clients, monkeypatch) -> None:
    """Maintenance preserves every unread message while consumers settle earlier ones."""
    monkeypatch.setattr("transport.infra.redis_gateway._MAINTENANCE_INTERVAL_SEC", 0.0)
    client = clients()
    stream = f"{_fresh_prefix()}:inbound"
    inbox = _inbox(client, stream)
    for index in range(5):
        inbox.submit(envelope(f"m{index}"))

    polled = [inbox.poll(POLL_TIMEOUT_SEC) for _ in range(5)]

    assert [item.text for item in polled if item is not None] == [f"m{index}" for index in range(5)]


def test_an_inbox_whose_group_vanished_recreates_it_and_keeps_draining(clients) -> None:
    """A Redis restart takes every group; caching "it exists" made that terminal.

    The supervisor's tick would raise on every poll for the rest of the process's life, and
    the envelopes still in the stream would reach nobody.
    """
    client = clients()
    stream = f"{_fresh_prefix()}:inbound"
    inbox = _inbox(client, stream)
    inbox.submit(envelope("before the group went"))
    assert inbox.poll(POLL_TIMEOUT_SEC) is not None

    client.xgroup_destroy(stream, "supervisor")
    inbox.submit(envelope("after the group went"))

    polled = [inbox.poll(POLL_TIMEOUT_SEC), inbox.poll(POLL_TIMEOUT_SEC)]

    assert "after the group went" in [item.text for item in polled if item is not None]


def test_an_envelope_submitted_before_any_consumer_existed_is_still_polled(clients) -> None:
    """The inbound group starts at the beginning: a submit is durable before a drain."""
    client = clients()
    stream = f"{_fresh_prefix()}:inbound"
    producer = _inbox(client, stream, consumer="transport")
    producer.submit(envelope("submitted before the supervisor started"))

    consumer = _inbox(client, stream)
    polled = consumer.poll(POLL_TIMEOUT_SEC)

    assert polled is not None
    assert polled.text == "submitted before the supervisor started"


# -- what a poll does when the broker is not there --------------------------


class _UnreachableClient:
    """A client whose every command fails, as one pointed at a Redis that is restarting."""

    def __init__(self) -> None:
        self.attempts = 0

    def __getattr__(self, name: str):
        import redis

        self.attempts += 1

        def _fail(*_args, **_kwargs):
            raise redis.exceptions.ConnectionError("the broker is not there")

        return _fail


def test_a_broker_that_is_not_there_returns_nothing_rather_than_crashing_the_tick(caplog) -> None:
    """A missing broker pauses the tick instead of escaping from the poll."""
    unreachable = _UnreachableClient()
    inbox = RedisInbox(
        client=unreachable,
        append_client=unreachable,
        stream="s:inbound",
        group="supervisor",
        consumer="supervisor-1",
    )

    with caplog.at_level(logging.WARNING, logger="transport.infra.redis_gateway"):
        started = time.monotonic()
        for _ in range(50):  # what the tick would do while Redis restarts
            assert inbox.poll(0.01) is None
        elapsed = time.monotonic() - started

    assert elapsed >= 0.4, "each poll must spend the timeout the tick meant to spend"
    assert unreachable.attempts <= 2, "a broker known to be down must not be dialled once per poll"
    assert len(caplog.records) == 1, "one outage is one report, not one per attempt"


def test_a_broker_that_comes_back_is_drained_without_a_restart(clients, monkeypatch, caplog) -> None:
    """The outage is a delay, not a latch: the tier must recover on its own, and say so.

    The recovery line is half the diagnosis — an operator told a tier stopped consuming has
    no other way to learn that it started again — and it is also what resets the report gate,
    so the *next* failure is reported with its own cause instead of inheriting this one's
    silence.
    """
    monkeypatch.setattr("transport.infra.redis_gateway.BROKER_RETRY_SEC", 0.05)
    import redis

    client = clients()
    stream = f"{_fresh_prefix()}:inbound"
    inbox = _inbox(client, stream)
    inbox.submit(envelope("submitted while the broker was up"))
    down = {"yes": True}
    inner_read = client.xreadgroup

    def _sometimes(*args, **kwargs):
        if down["yes"]:
            raise redis.exceptions.ConnectionError("the broker is not there yet")
        return inner_read(*args, **kwargs)

    client.xreadgroup = _sometimes  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="transport.infra.redis_gateway"):
        assert inbox.poll(0.01) is None

        down["yes"] = False
        time.sleep(0.06)

        polled = inbox.poll(POLL_TIMEOUT_SEC)

    assert polled is not None and polled.text == "submitted while the broker was up"
    assert [record.getMessage() for record in caplog.records][-1].endswith("can be read again")


# -- an envelope nobody can read --------------------------------------------


def _pending_ids(client, stream: str, consumer: str = "supervisor-1") -> list[str]:
    entries = client.xpending_range(stream, "supervisor", min="-", max="+", count=32, consumername=consumer)
    return [str(_text(_row(item).get("message_id"))) for item in entries]


def test_an_inbound_entry_that_cannot_be_decoded_is_acknowledged_and_dropped(clients, caplog) -> None:
    """Unacknowledged it stays in this consumer name's pending list for good.

    The outbound half has had this case since it was written; the inbound one had none, and
    on this side the entry comes back on every iteration of the drain rather than once.
    """
    client = clients()
    stream = f"{_fresh_prefix()}:inbound"
    inbox = _inbox(client, stream)
    unreadable = str(client.xadd(stream, {"payload": "{not json"}))
    inbox.submit(envelope("the one after it"))

    with caplog.at_level(logging.ERROR, logger="transport.infra.redis_gateway"):
        polled = inbox.poll(POLL_TIMEOUT_SEC)

    assert polled is not None and polled.text == "the one after it"
    assert any("does not decode" in record.getMessage() for record in caplog.records)
    assert unreadable not in _pending_ids(client, stream), "a dropped envelope must not stay owed to this consumer"


def test_an_unreadable_envelope_inherited_from_a_dead_instance_is_settled_rather_than_re_read(clients, caplog) -> None:
    """The redelivery `_drain_pending` exists for is also how an unreadable entry recurs.

    An instance takes an envelope, dies before settling it, and its replacement presents the
    same consumer name — which is the whole point of that name. The replacement reads the
    pending list before anything else and gets the same entry on every iteration, past the
    deadline check, so without the acknowledgement the poll never returns at all.
    """
    client = clients()
    stream = f"{_fresh_prefix()}:inbound"
    unreadable = str(client.xadd(stream, {"payload": "{not json"}))
    client.xgroup_create(stream, "supervisor", id="0", mkstream=True)
    taken = first_entry(client.xreadgroup("supervisor", "supervisor-1", {stream: ">"}, count=1))
    assert taken is not None and taken[0] == unreadable, "the instance that died took exactly this one"
    _inbox(client, stream, consumer="transport").submit(envelope("the one after it"))

    replacement = _inbox(client, stream, consumer="supervisor-1")
    with caplog.at_level(logging.ERROR, logger="transport.infra.redis_gateway"):
        polled = replacement.poll(POLL_TIMEOUT_SEC)

    assert polled is not None and polled.text == "the one after it"
    assert unreadable not in _pending_ids(client, stream), "an inherited entry nobody can read must be settled"


def test_a_backlog_of_unreadable_envelopes_returns_to_the_tick_rather_than_owning_the_thread(clients) -> None:
    """The skip is a `continue` past the deadline check, so it needs a bound of its own."""
    client = clients()
    stream = f"{_fresh_prefix()}:inbound"
    inbox = _inbox(client, stream)
    for _ in range(MAX_SKIPPED_ENTRIES_PER_POLL + 4):
        client.xadd(stream, {"payload": "{not json"})
    inbox.submit(envelope("the one after them"))

    assert inbox.poll(0.0) is None, "one poll may not drain an unbounded backlog of them"

    assert inbox.poll(POLL_TIMEOUT_SEC).text == "the one after them"


def test_an_outbox_reader_survives_a_broker_that_is_not_there(caplog) -> None:
    """The delivery half needs the same promise the inbox makes, for a worse consequence.

    A raise out of this poll reaches `transport/reader_loop.py`, which stops consuming and
    hands back to a caller that closes the reader — and a new WebSocket connection cannot
    reopen it, because `attach_gateway` turns away the gateway it already has. That instance
    then delivers nothing for the rest of its life while its readiness probe answers 200.
    """
    from transport.infra.redis_gateway import _RedisOutboxReader

    unreachable = _UnreachableClient()
    # Built directly: the factory dials the broker to sweep and claim, so a client that is
    # already unreachable never gets far enough to hand one back. What is under test is the
    # reader's own poll.
    reader = _RedisOutboxReader(
        client=unreachable,
        stream="s:outbound",
        group="browser",
        consumer="browser#1",
        released_consumer="browser#0",
        mode=SubscriptionMode.BROADCAST,
        heartbeat_key="hb:browser",
    )

    with caplog.at_level(logging.WARNING, logger="transport.infra.redis_gateway"):
        for _ in range(20):
            assert reader.poll(0.01) is None

    assert unreachable.attempts <= 3, "a broker known to be down must not be dialled once per poll"
    outages = [record for record in caplog.records if "cannot be read" in record.getMessage()]
    assert len(outages) == 1, "one outage is one report, not one per attempt"


def test_guarded_publication_bounds_real_sentinel_discovery():
    import socket
    import threading
    import time

    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    from redis.sentinel import Sentinel

    from new_core.identity import OwnerScope
    from transport.infra.redis_gateway import RedisOutbox
    from transport.interfaces import OutboundMessage

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(2)
    port = listener.getsockname()[1]
    stop = threading.Event()
    received = threading.Event()

    def sink():
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(1)
            connection.recv(65536)
            received.set()
            stop.wait(2)

    thread = threading.Thread(target=sink)
    thread.start()
    sentinel = Sentinel(
        [("127.0.0.1", port)],
        sentinel_kwargs={"socket_connect_timeout": 0.6, "socket_timeout": 0.6, "retry": Retry(NoBackoff(), 0)},
        socket_connect_timeout=0.6,
        socket_timeout=0.6,
        retry=Retry(NoBackoff(), 0),
    )
    append = sentinel.master_for("review")
    client = redis.Redis(host="127.0.0.1", port=1)
    outbox = RedisOutbox(client=client, append_client=append, stream="unused-sentinel-review")
    start = time.monotonic()
    deadline = start + 0.05

    def guard():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline expired")
        return remaining

    try:
        with pytest.raises((TimeoutError, redis.ConnectionError)):
            outbox.publish(
                OutboundMessage(
                    scope=OwnerScope(owner_id="review", agent_id="review", surface_id="browser", chat_id="review"),
                    source="browser",
                    text="review",
                ),
                before_publish=guard,
            )
        elapsed = time.monotonic() - start
        assert elapsed < 0.2, (
            "Sentinel discovery I/O retained source-client timeout instead of remaining publication budget"
        )
        assert received.is_set(), "the test must reach actual Sentinel I/O"
        assert sentinel.sentinels[0].connection_pool.connection_kwargs["socket_timeout"] == 0.6
    finally:
        stop.set()
        thread.join(3)
        listener.close()
        append.close()
        append.connection_pool.disconnect()
        for sentinel_client in sentinel.sentinels:
            sentinel_client.close()
        client.close()
