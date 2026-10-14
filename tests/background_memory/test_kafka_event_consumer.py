from __future__ import annotations

import asyncio
import logging
import ssl
import time

import pytest
from aiokafka import TopicPartition
from aiokafka.errors import CommitFailedError, KafkaTimeoutError, RebalanceInProgressError

import infra.kafka_client as kafka_client
from background_memory.infra.events.kafka_transport import (
    KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS,
    MAX_SKIPPED_RECORDS_PER_POLL,
    KafkaMemoryEventConsumer,
    KafkaMemoryEventPublisher,
)
from infra.kafka_client import KafkaConnection, KafkaOffsetReset
from new_core.identity import OwnerScope
from ouroboros.contracts.stream_replies import BROKER_RETRY_SEC
from tests.helpers.kafka_helpers import (
    create_broker_topics,
    fleet_topics,
    fresh_prefix,
    needs_kafka,
    real_client_factory,
    release_real_broker,
)
from tests.helpers.kafka_memory_events import (
    CONNECTION,
    MODULE,
    delivered_task_ids,
    fleet_double,
    memory_consumer,
    memory_group,
    memory_publisher,
    memory_topic,
    partition_of,
    record_every_position,
    start_consumers,
)
from tests.helpers.kafka_recording import KafkaRecordingClientFactory, RecordingClient
from tests.helpers.memory_event_conformance import (
    DELIVERY_TIMEOUT_SEC,
    QUIET_POLL_SEC,
    distillation_event,
    poll,
    task_id_of,
)
from tests.helpers.shared import TEST_SCOPE, scope_with

TOPIC = memory_topic(CONNECTION)
GROUP = memory_group(CONNECTION)
LAPSE_INTERVAL_MS = 6_000
LAPSE_MARGIN_SEC = 1.0
SKIP_RUN_BOUND_SEC = 1.0
SILENT_BROKER_MARGIN_SEC = 1.0
JOIN_BOUND_UNDER_TEST_SEC = 2.0
OWNERS = [scope_with(TEST_SCOPE, owner_id=f"owner-{index}") for index in range(8)]


def _warmed(
    clients: KafkaRecordingClientFactory,
    client_id: str = "background-memory-0",
    *,
    scopes: tuple[OwnerScope, ...] = (TEST_SCOPE,),
) -> tuple[KafkaMemoryEventPublisher, KafkaMemoryEventConsumer]:
    publisher = memory_publisher(clients, CONNECTION)
    consumer = memory_consumer(clients, CONNECTION, client_id=client_id)
    start_consumers(clients, [consumer])
    warm_ups = [f"warm-up-{index}" for index in range(len(scopes))]
    for task_id, scope in zip(warm_ups, scopes, strict=True):
        publisher.publish(distillation_event(task_id, scope))
    assert sorted(str(task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC))) for _ in scopes) == warm_ups
    return publisher, consumer


def _group_consumer_of(clients: KafkaRecordingClientFactory, client_id: str) -> RecordingClient:
    built = [recorded.client for recorded in clients.clients_of("async_group_consumer")]
    return next(client for client in built if client.wrapped.client_id == client_id)


def _other_partition_scope() -> OwnerScope:
    home = partition_of(TEST_SCOPE)
    return next(scope for scope in OWNERS if partition_of(scope) != home)


def test_the_position_is_recorded_before_the_event_is_returned() -> None:
    cluster, clients = fleet_double()
    publisher, consumer = _warmed(clients)
    partition = partition_of(TEST_SCOPE)
    publisher.publish(distillation_event("recorded"))
    offset = cluster.end_offset(TOPIC, partition) - 1

    assert task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC)) == "recorded"
    assert cluster.committed(GROUP, TOPIC, partition) == offset + 1


def test_the_position_past_a_skipped_record_is_recorded() -> None:
    cluster, clients = fleet_double()
    publisher, consumer = _warmed(clients)
    partition = partition_of(TEST_SCOPE)
    skipped = cluster.append_raw(TOPIC, partition, b"{not json", key=KafkaMemoryEventPublisher.record_key(TEST_SCOPE))
    publisher.publish(distillation_event("after-it"))

    assert task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC)) == "after-it"
    assert cluster.committed(GROUP, TOPIC, partition) == skipped + 2


@pytest.mark.parametrize(
    "failure",
    [KafkaTimeoutError, lambda: ssl.SSLError("tlsv13 alert certificate required")],
    ids=["a timed-out commit", "a tls failure outside the kafka error hierarchy"],
)
def test_a_failed_recording_still_delivers_and_is_recorded_again_before_anything_further(failure) -> None:
    cluster, clients = fleet_double()
    publisher, consumer = _warmed(clients)
    group_consumer = _group_consumer_of(clients, "background-memory-0")
    partition = partition_of(TEST_SCOPE)
    publisher.publish(distillation_event("unrecorded"))
    publisher.publish(distillation_event("behind-it"))
    unrecorded = cluster.end_offset(TOPIC, partition) - 2
    group_consumer.fail_next("commit", failure())

    assert task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC)) == "unrecorded"
    assert cluster.committed(GROUP, TOPIC, partition) == unrecorded
    issued = len(group_consumer.calls)
    assert task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC)) == "behind-it"

    calls = group_consumer.calls[issued:]
    assert [call.method for call in calls] == ["commit", "getmany", "commit"]
    # This library takes the bare next offset, where `kafka-python` took a triple.
    assert calls[0].args[0] == {TopicPartition(TOPIC, partition): unrecorded + 1}
    assert cluster.committed(GROUP, TOPIC, partition) == unrecorded + 2


def test_while_the_recording_keeps_failing_nothing_is_fetched_and_one_outage_is_reported(caplog) -> None:
    cluster, clients = fleet_double()
    publisher, consumer = _warmed(clients)
    group_consumer = _group_consumer_of(clients, "background-memory-0")
    publisher.publish(distillation_event("unrecorded"))
    publisher.publish(distillation_event("held-back"))
    group_consumer.fail_next("commit", KafkaTimeoutError())
    assert task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC)) == "unrecorded"
    cluster.set_outage(True)
    fetches = len(group_consumer.calls_of("getmany"))
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger=MODULE):
        polled = [poll(consumer, 0.01) for _ in range(20)]
    reported = list(caplog.records)
    cluster.set_outage(False)
    time.sleep(BROKER_RETRY_SEC + 0.1)

    assert polled == [None] * 20
    assert len(group_consumer.calls_of("getmany")) == fetches
    assert len(reported) == 1 and reported[0].exc_info
    assert delivered_task_ids(consumer) == ["held-back"]


@pytest.mark.parametrize("refusal", [CommitFailedError, RebalanceInProgressError])
def test_a_stale_recording_is_dropped_with_a_warning_and_never_retried(refusal, caplog) -> None:
    _cluster, clients = fleet_double()
    publisher, consumer = _warmed(clients)
    group_consumer = _group_consumer_of(clients, "background-memory-0")
    publisher.publish(distillation_event("stale"))
    group_consumer.fail_next("commit", refusal())

    with caplog.at_level(logging.WARNING, logger=MODULE):
        assert task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC)) == "stale"
    publisher.publish(distillation_event("next"))
    commits = len(group_consumer.calls_of("commit"))

    assert task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC)) == "next"
    assert len(group_consumer.calls_of("commit")) == commits + 1
    assert any(refusal.__name__ in record.getMessage() for record in caplog.records)


def test_a_partition_without_a_recorded_position_begins_at_the_present_on_its_next_owner() -> None:
    cluster, clients = fleet_double()
    publisher, _first_owner = _warmed(clients, "background-memory-a")
    other = _other_partition_scope()
    publisher.publish(distillation_event("home-0"))
    publisher.publish(distillation_event("other-0", other))

    cluster.crash(_group_consumer_of(clients, "background-memory-a").wrapped)
    next_owner = memory_consumer(clients, CONNECTION, client_id="background-memory-b")
    resumed = task_id_of(poll(next_owner, DELIVERY_TIMEOUT_SEC))
    publisher.publish(distillation_event("other-1", other))

    assert resumed == "home-0"
    assert delivered_task_ids(next_owner) == ["other-1"]


def test_a_failed_recording_before_a_crash_repeats_only_that_event() -> None:
    cluster, clients = fleet_double()
    other = _other_partition_scope()
    publisher, first_owner = _warmed(clients, "background-memory-a", scopes=(TEST_SCOPE, other))
    published = ["home-0", "home-1", "other-0", "other-1"]
    for task_id in published:
        publisher.publish(distillation_event(task_id, TEST_SCOPE if task_id.startswith("home") else other))
    crashed = _group_consumer_of(clients, "background-memory-a")
    crashed.fail_next("commit", KafkaTimeoutError())
    taken = task_id_of(poll(first_owner, DELIVERY_TIMEOUT_SEC))

    cluster.crash(crashed.wrapped)
    delivered = delivered_task_ids(memory_consumer(clients, CONNECTION, client_id="background-memory-b"))

    assert taken in published
    assert sorted(str(task_id) for task_id in delivered) == sorted(published)


def test_a_crashed_member_drops_its_stale_position_and_receives_again(caplog) -> None:
    cluster, clients = fleet_double()
    publisher, consumer = _warmed(clients)
    group_consumer = _group_consumer_of(clients, "background-memory-0")
    publisher.publish(distillation_event("unrecorded"))
    group_consumer.fail_next("commit", KafkaTimeoutError())
    assert task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC)) == "unrecorded"
    cluster.crash(group_consumer.wrapped)
    publisher.publish(distillation_event("after-the-rejoin"))

    with caplog.at_level(logging.WARNING, logger=MODULE):
        delivered = delivered_task_ids(consumer)

    assert delivered == ["unrecorded", "after-the-rejoin"]
    assert any("CommitFailedError" in record.getMessage() for record in caplog.records)


def test_an_evicted_position_resumes_at_the_present_without_raising() -> None:
    cluster, clients = fleet_double()
    publisher, _consumer = _warmed(clients, "background-memory-a")
    partition = partition_of(TEST_SCOPE)
    cluster.crash(_group_consumer_of(clients, "background-memory-a").wrapped)
    for task_id in ("evicted-0", "evicted-1", "retained"):
        publisher.publish(distillation_event(task_id))
    cluster.delete_up_to(TOPIC, partition, cluster.end_offset(TOPIC, partition) - 1)
    consumer = memory_consumer(clients, CONNECTION, client_id="background-memory-b")

    assert poll(consumer, QUIET_POLL_SEC) is None
    publisher.publish(distillation_event("after-the-read"))

    assert delivered_task_ids(consumer) == ["after-the-read"]


def test_a_group_whose_positions_were_discarded_begins_at_the_present() -> None:
    cluster, clients = fleet_double()
    publisher, _consumer = _warmed(clients, "background-memory-a")
    cluster.crash(_group_consumer_of(clients, "background-memory-a").wrapped)
    publisher.publish(distillation_event("while-the-positions-were-missing"))
    cluster.discard_positions(GROUP)
    consumer = memory_consumer(clients, CONNECTION, client_id="background-memory-b")

    assert poll(consumer, QUIET_POLL_SEC) is None
    publisher.publish(distillation_event("after-the-start"))

    assert delivered_task_ids(consumer) == ["after-the-start"]


def test_a_tls_failure_from_the_group_consumer_is_recorded_as_an_outage_rather_than_raised(caplog) -> None:
    _cluster, clients = fleet_double()
    _publisher, consumer = _warmed(clients)
    _group_consumer_of(clients, "background-memory-0").fail_next(
        "getmany", ssl.SSLCertVerificationError("certificate verify failed")
    )

    with caplog.at_level(logging.WARNING, logger=MODULE):
        assert poll(consumer, 0.01) is None

    assert len(caplog.records) == 1 and caplog.records[0].exc_info
    assert TOPIC in caplog.records[0].getMessage() and "SSLCertVerificationError" in caplog.records[0].getMessage()


def test_an_idle_consumer_asks_the_broker_nothing_beyond_its_fetch(caplog) -> None:
    cluster, clients = fleet_double()
    _publisher, consumer = _warmed(clients)
    group_consumer = _group_consumer_of(clients, "background-memory-0")
    issued = len(group_consumer.calls)
    built = len(clients.built)

    with caplog.at_level(logging.WARNING, logger=MODULE):
        for outage in (False, True):
            cluster.set_outage(outage)
            assert [poll(consumer, 0.05) for _ in range(5)] == [None] * 5
        cluster.set_outage(False)

    assert {call.method for call in group_consumer.calls[issued:]} == {"getmany"}
    assert len(clients.built) == built
    assert caplog.records == []


def test_the_first_poll_builds_one_group_consumer_with_the_constructors_client_id() -> None:
    _cluster, clients = fleet_double()
    consumer = memory_consumer(clients, CONNECTION, client_id="background-memory-7")
    assert clients.clients_of("async_group_consumer") == []

    poll(consumer)
    poll(consumer)

    built = clients.clients_of("async_group_consumer")
    assert [recorded.kwargs for recorded in built] == [
        {
            "group_id": GROUP,
            "client_id": "background-memory-7",
            "auto_offset_reset": KafkaOffsetReset.LATEST,
            "max_poll_interval_ms": KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS,
        }
    ]
    assert [call.args for call in built[0].client.calls_of("subscribe")] == [([TOPIC],)]


def test_a_zero_timeout_read_returns_an_event_published_just_before_it() -> None:
    _cluster, clients = fleet_double()
    publisher, consumer = _warmed(clients)
    publisher.publish(distillation_event("just-before"))

    assert task_id_of(poll(consumer, 0)) == "just-before"


def _lapse_hands_the_work_over(clients: KafkaRecordingClientFactory, connection: KafkaConnection, margin_sec: float):
    publisher = memory_publisher(clients, connection)
    lapsing, peer = (
        memory_consumer(clients, connection, client_id=client_id, max_poll_interval_ms=LAPSE_INTERVAL_MS)
        for client_id in ("background-memory-a", "background-memory-b")
    )
    start_consumers(clients, [lapsing, peer])
    record_every_position(publisher, [lapsing, peer])
    for index, scope in enumerate(OWNERS):
        publisher.publish(distillation_event(f"before-{index}", scope))
    in_hand = task_id_of(poll(lapsing, DELIVERY_TIMEOUT_SEC))
    assert in_hand is not None

    by_peer: list[str | None] = []
    lapse_ends = time.monotonic() + LAPSE_INTERVAL_MS / 1000 + margin_sec
    while time.monotonic() < lapse_ends:
        polled = poll(peer, QUIET_POLL_SEC)
        if polled is not None:
            by_peer.append(task_id_of(polled))
    for index, scope in enumerate(OWNERS):
        publisher.publish(distillation_event(f"after-{index}", scope))
    by_peer += delivered_task_ids(peer)

    assert sorted(str(task_id) for task_id in by_peer) == sorted(
        [f"before-{index}" for index in range(len(OWNERS)) if f"before-{index}" != in_hand]
        + [f"after-{index}" for index in range(len(OWNERS))]
    )

    start_consumers(clients, [lapsing, peer])
    for index, scope in enumerate(OWNERS):
        publisher.publish(distillation_event(f"rejoined-{index}", scope))
    by_lapsed = delivered_task_ids(lapsing)
    rest = delivered_task_ids(peer)
    assert by_lapsed
    assert sorted(str(task_id) for task_id in by_lapsed + rest) == sorted(
        f"rejoined-{index}" for index in range(len(OWNERS))
    )


def test_a_delivery_that_outlasts_the_interval_hands_the_work_over_without_replaying_it() -> None:
    _cluster, clients = fleet_double()

    _lapse_hands_the_work_over(clients, CONNECTION, LAPSE_MARGIN_SEC)


def test_a_lapse_after_a_failed_recording_repeats_only_that_event() -> None:
    _cluster, clients = fleet_double()
    publisher = memory_publisher(clients, CONNECTION)
    lapsing, peer = (
        memory_consumer(clients, CONNECTION, client_id=client_id, max_poll_interval_ms=LAPSE_INTERVAL_MS)
        for client_id in ("background-memory-a", "background-memory-b")
    )
    start_consumers(clients, [lapsing, peer])
    record_every_position(publisher, [lapsing, peer])
    published = [f"before-{index}" for index in range(len(OWNERS))]
    for task_id, scope in zip(published, OWNERS, strict=True):
        publisher.publish(distillation_event(task_id, scope))
    _group_consumer_of(clients, "background-memory-a").fail_next("commit", KafkaTimeoutError())
    in_hand = task_id_of(poll(lapsing, DELIVERY_TIMEOUT_SEC))
    assert in_hand in published

    by_peer: list[str | None] = []
    lapse_ends = time.monotonic() + LAPSE_INTERVAL_MS / 1000 + LAPSE_MARGIN_SEC
    while time.monotonic() < lapse_ends:
        polled = poll(peer, QUIET_POLL_SEC)
        if polled is not None:
            by_peer.append(task_id_of(polled))
    delivered_again = by_peer + delivered_task_ids(peer) + delivered_task_ids(lapsing)

    assert sorted(str(task_id) for task_id in delivered_again) == sorted(published)


class TestRealKafkaMemoryLapse:
    pytestmark = needs_kafka

    def test_a_delivery_that_outlasts_the_interval_hands_the_work_over_without_replaying_it(self) -> None:
        prefix = fresh_prefix()
        factory = real_client_factory(prefix)
        clients = KafkaRecordingClientFactory(clients=factory)
        try:
            create_broker_topics(fleet_topics(factory.connection))
            heartbeat_sec = kafka_client._GROUP_HEARTBEAT_INTERVAL_MS / 1000
            _lapse_hands_the_work_over(clients, factory.connection, LAPSE_MARGIN_SEC + heartbeat_sec)
        finally:
            release_real_broker(clients, prefix)


def test_a_zero_timeout_poll_over_a_skip_run_does_not_spend_the_first_fetch_floor_again() -> None:
    cluster, clients = fleet_double()
    publisher, consumer = _warmed(clients)
    partition = partition_of(TEST_SCOPE)
    key = KafkaMemoryEventPublisher.record_key(TEST_SCOPE)
    for _ in range(MAX_SKIPPED_RECORDS_PER_POLL):
        cluster.append_raw(TOPIC, partition, b"{not json", key=key)
    publisher.publish(distillation_event("behind the skip run"))

    started = time.monotonic()
    assert poll(consumer, 0.0) is None
    elapsed = time.monotonic() - started

    fetches = _group_consumer_of(clients, "background-memory-0").calls_of("getmany")
    waits = [call.kwargs["timeout_ms"] for call in fetches][-MAX_SKIPPED_RECORDS_PER_POLL:]
    assert waits.count(kafka_client.KAFKA_ZERO_TIMEOUT_FETCH_MS) <= 1
    assert elapsed < SKIP_RUN_BOUND_SEC


def test_an_idle_poll_that_answers_clears_an_outage_no_record_had_to_end(caplog) -> None:
    _cluster, clients = fleet_double()
    _publisher, consumer = _warmed(clients)
    _group_consumer_of(clients, "background-memory-0").fail_next(
        "getmany", ssl.SSLCertVerificationError("certificate verify failed")
    )
    assert poll(consumer, 0.0) is None

    time.sleep(BROKER_RETRY_SEC + 0.1)
    with caplog.at_level(logging.WARNING, logger=MODULE):
        assert poll(consumer, 0.0) is None

    assert [record for record in caplog.records if "can be read again" in record.getMessage()]


# --- the read does not occupy the loop it is awaited on -------------------------


def test_an_awaited_read_on_an_empty_stream_lets_other_work_on_the_loop_run() -> None:
    """The whole point of the second client library: the interim one held the loop for the fetch."""
    _cluster, clients = fleet_double()
    _publisher, consumer = _warmed(clients)
    ticks = 0

    async def _ticking() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    async def _scenario() -> None:
        ticker = asyncio.ensure_future(_ticking())
        try:
            assert await consumer.poll(1.0) is None
        finally:
            ticker.cancel()

    asyncio.get_event_loop().run_until_complete(_scenario())

    assert ticks > 10, f"the read occupied the loop for its whole duration (ticks={ticks})"


def test_a_read_against_a_silent_broker_returns_within_its_timeout() -> None:
    """A cluster that answers nothing must still hand the loop back on the caller's budget."""
    cluster, clients = fleet_double()
    _publisher, consumer = _warmed(clients)
    cluster.set_outage(True)

    started = time.monotonic()
    assert poll(consumer, 0.3) is None
    spent = time.monotonic() - started

    assert spent < 0.3 + SILENT_BROKER_MARGIN_SEC, f"a silent broker held the read for {spent:.2f}s"


def test_a_read_on_a_consumer_that_was_never_started_raises() -> None:
    """`ConsumerStoppedError` is not in the unanswered tuple: it is a programming error."""
    from aiokafka.errors import ConsumerStoppedError

    _cluster, clients = fleet_double()
    member = clients.async_group_consumer(
        group_id=GROUP,
        client_id="never-started",
        auto_offset_reset=KafkaOffsetReset.LATEST,
        max_poll_interval_ms=KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS,
    )

    async def _read() -> None:
        await member.getmany(timeout_ms=0)

    with pytest.raises(ConsumerStoppedError):
        asyncio.get_event_loop().run_until_complete(_read())


def test_stopping_the_consumer_releases_its_group_membership() -> None:
    _cluster, clients = fleet_double()
    _publisher, consumer = _warmed(clients)
    member = _group_consumer_of(clients, "background-memory-0").wrapped

    asyncio.get_event_loop().run_until_complete(consumer.close())

    assert member.assignment() == set()
    assert consumer._consumer is None


def test_a_join_that_never_completes_is_given_up_on_rather_than_awaited(monkeypatch, caplog) -> None:
    """Topics belong to the infrastructure: a reader must not wait minutes for one to appear."""
    _cluster, clients = fleet_double()
    consumer = memory_consumer(clients, CONNECTION, client_id="background-memory-slow")

    async def _never(self) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(kafka_client, "KAFKA_JOIN_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr("tests.helpers.kafka_double_async.DoubleAsyncGroupConsumer.start", _never)

    with caplog.at_level(logging.WARNING, logger=MODULE):
        started = time.monotonic()
        assert poll(consumer, 0.0) is None
        assert poll(consumer, 0.5) is None
        assert time.monotonic() - started < 2.0

    assert consumer._consumer is None, "the consumer that never joined was kept"
    assert any("cannot be read" in record.getMessage() for record in caplog.records)
    asyncio.get_event_loop().run_until_complete(consumer.close())


def test_a_first_read_answers_on_its_own_timeout_rather_than_the_joins(monkeypatch) -> None:
    """The loop's pass is 0.2s; joining a group is not, and it may not be charged to the pass."""
    _cluster, clients = fleet_double()
    consumer = memory_consumer(clients, CONNECTION, client_id="background-memory-joining")
    attempted = asyncio.Event()

    async def _slow(self) -> None:
        attempted.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(kafka_client, "KAFKA_JOIN_TIMEOUT_SEC", JOIN_BOUND_UNDER_TEST_SEC)
    monkeypatch.setattr("tests.helpers.kafka_double_async.DoubleAsyncGroupConsumer.start", _slow)

    started = time.monotonic()
    assert poll(consumer, QUIET_POLL_SEC) is None
    spent = time.monotonic() - started

    assert spent < JOIN_BOUND_UNDER_TEST_SEC, f"the pass waited the join's own bound out ({spent:.2f}s)"
    assert attempted.is_set(), "the join was not attempted at all"
    assert consumer._consumer is None
    asyncio.get_event_loop().run_until_complete(consumer.close())


def test_a_commit_that_never_answers_is_given_up_on_within_its_bound(monkeypatch) -> None:
    """This library's `commit` takes no deadline of its own: a retriable broker retries forever."""
    import background_memory.infra.events.kafka_transport as transport

    cluster, clients = fleet_double()
    publisher, consumer = _warmed(clients)
    partition = partition_of(TEST_SCOPE)
    publisher.publish(distillation_event("unanswered-commit"))
    offset = cluster.end_offset(TOPIC, partition) - 1
    reached = asyncio.Event()

    async def _never(self, offsets=None) -> None:
        reached.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(transport, "_COMMIT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr("tests.helpers.kafka_double_async.DoubleAsyncGroupConsumer.commit", _never)

    started = time.monotonic()
    assert task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC)) == "unanswered-commit"
    spent = time.monotonic() - started

    assert spent < DELIVERY_TIMEOUT_SEC, f"the read waited the commit out ({spent:.2f}s)"
    assert reached.is_set(), "the commit was never attempted"
    assert consumer._pending == {TopicPartition(TOPIC, partition): offset + 1}
    assert cluster.committed(GROUP, TOPIC, partition) != offset + 1
