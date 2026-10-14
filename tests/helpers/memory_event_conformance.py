from __future__ import annotations

import asyncio
import logging
import pathlib
import time
from collections.abc import Sequence

import pytest

from background_memory.infra.events import memory_event_publisher_factory
from background_memory.interfaces import (
    ConsolidationContinuationEvent,
    IMemoryEventConsumer,
    IMemoryEventPublisher,
    MemoryDistillationEvent,
    MemoryEvent,
    MemoryTaskRef,
    NullMemoryEventPublisher,
)
from new_core.identity import OwnerScope
from ouroboros.contracts.stream_replies import BROKER_RETRY_SEC
from tests.helpers.shared import TEST_SCOPE, scope_with

DELIVERY_TIMEOUT_SEC = 2.0
QUIET_POLL_SEC = 0.2
IMMEDIATE_SEC = 1.0

MemoryEventPair = tuple[IMemoryEventPublisher, IMemoryEventConsumer]


def poll(consumer: IMemoryEventConsumer, timeout: float = 0.0) -> MemoryEvent | None:
    """Drive the awaitable consumer from a synchronous test.

    The seam's consuming half is a coroutine and its publishing half is not, so a case that
    holds both drives the reader through here rather than through a second async test style.
    """
    return asyncio.get_event_loop().run_until_complete(consumer.poll(timeout))


def reset_memory_event_publisher() -> None:
    memory_event_publisher_factory.initialize_broker(NullMemoryEventPublisher())


def distillation_event(task_id: str = "src1", scope: OwnerScope = TEST_SCOPE) -> MemoryDistillationEvent:
    return MemoryDistillationEvent(
        scope=scope,
        task=MemoryTaskRef(task_id=task_id, task_type="task", text="do it", recorded_at=1.5),
        response_text="A" * 200,
        llm_trace={"tool_calls": []},
    )


def task_id_of(event: MemoryEvent | None) -> str | None:
    return event.task.task_id if isinstance(event, MemoryDistillationEvent) else None


class MemoryEventConformance:
    def make_publisher_and_consumer(self) -> MemoryEventPair:
        raise NotImplementedError("a subclass builds a publisher and a consumer that has completed its first read")

    def second_consumer(self, consumer: IMemoryEventConsumer) -> IMemoryEventConsumer:
        raise NotImplementedError("a subclass builds another instance of the tier in the same group")

    def make_group_after_history(self, history: Sequence[MemoryEvent]) -> MemoryEventPair | None:
        raise NotImplementedError("a subclass declares how its group's start is established after history")

    def break_broker(self, publisher: IMemoryEventPublisher, consumer: IMemoryEventConsumer) -> bool:
        raise NotImplementedError("a subclass declares whether it can make its broker unreachable")

    def heal_broker(self, publisher: IMemoryEventPublisher, consumer: IMemoryEventConsumer) -> None:
        raise NotImplementedError("a subclass that breaks its broker declares how it heals it")

    def inject_raw_record(self, consumer: IMemoryEventConsumer, payload: bytes) -> bool:
        raise NotImplementedError("a subclass declares whether a record no publisher writes can reach its consumer")

    def max_skipped_per_poll(self) -> int:
        raise NotImplementedError("a subclass that accepts raw records declares its per-read skip bound")

    def serializes_events(self) -> bool:
        raise NotImplementedError("a subclass declares whether publishing encodes through the wire codec")

    def test_both_halves_satisfy_their_own_interface_only(self) -> None:
        publisher, consumer = self.make_publisher_and_consumer()

        assert isinstance(publisher, IMemoryEventPublisher)
        assert isinstance(consumer, IMemoryEventConsumer)
        assert not isinstance(publisher, IMemoryEventConsumer)
        assert not isinstance(consumer, IMemoryEventPublisher)

    def test_a_published_event_round_trips_intact(self) -> None:
        publisher, consumer = self.make_publisher_and_consumer()
        event = distillation_event()

        publisher.publish(event)
        polled = poll(consumer, DELIVERY_TIMEOUT_SEC)

        assert isinstance(polled, MemoryDistillationEvent)
        assert polled == event
        assert polled.task.recorded_at == 1.5
        assert polled.scope == event.scope

    def test_every_published_event_is_delivered(self) -> None:
        publisher, consumer = self.make_publisher_and_consumer()
        published = [distillation_event(f"src{index}") for index in range(20)]

        for event in published:
            publisher.publish(event)

        delivered: list[str | None] = []
        while (polled := poll(consumer, DELIVERY_TIMEOUT_SEC if not delivered else QUIET_POLL_SEC)) is not None:
            delivered.append(task_id_of(polled))

        assert sorted(str(task_id) for task_id in delivered) == sorted(event.task.task_id for event in published)

    def test_two_instances_of_the_tier_share_the_work(self) -> None:
        publisher, consumer = self.make_publisher_and_consumer()
        readers = [consumer, self.second_consumer(consumer)]
        published = [f"src{index}" for index in range(6)]
        for index, task_id in enumerate(published):
            publisher.publish(distillation_event(task_id, scope_with(TEST_SCOPE, owner_id=f"owner-{index}")))

        delivered: list[str | None] = []
        deadline = time.monotonic() + 4 * DELIVERY_TIMEOUT_SEC
        while len(delivered) < len(published) and time.monotonic() < deadline:
            for reader in readers:
                polled = poll(reader, QUIET_POLL_SEC)
                if polled is not None:
                    delivered.append(task_id_of(polled))
        extra = [poll(reader, QUIET_POLL_SEC) for reader in readers]

        assert sorted(str(task_id) for task_id in delivered) == published
        assert extra == [None, None]

    def test_a_fresh_group_does_not_replay_history(self) -> None:
        made = self.make_group_after_history([distillation_event("published-before-the-group-began")])
        if made is None:
            pytest.skip(f"{type(self).__name__} declares no group whose start follows history")
        publisher, consumer = made

        publisher.publish(distillation_event("published-after-the-start"))
        delivered: list[str | None] = []
        while (polled := poll(consumer, DELIVERY_TIMEOUT_SEC if not delivered else QUIET_POLL_SEC)) is not None:
            delivered.append(task_id_of(polled))

        assert delivered == ["published-after-the-start"]

    def test_an_undecodable_record_does_not_block_the_group(self, caplog: pytest.LogCaptureFixture) -> None:
        publisher, consumer = self.make_publisher_and_consumer()
        if not self.inject_raw_record(consumer, b"{not json"):
            pytest.skip(f"{type(self).__name__} declares that no record but a published event reaches its consumer")
        publisher.publish(distillation_event("the-one-after-it"))

        with caplog.at_level(logging.WARNING):
            polled = poll(consumer, DELIVERY_TIMEOUT_SEC)

        assert task_id_of(polled) == "the-one-after-it"
        assert any("does not decode" in record.getMessage() for record in caplog.records)

    def test_an_unknown_kind_tag_is_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        publisher, consumer = self.make_publisher_and_consumer()
        if not self.inject_raw_record(consumer, b'{"kind": "SomethingTheReaderHasNeverHeardOf"}'):
            pytest.skip(f"{type(self).__name__} declares that no record but a published event reaches its consumer")
        publisher.publish(distillation_event("the-one-after-it"))

        with caplog.at_level(logging.WARNING):
            polled = poll(consumer, DELIVERY_TIMEOUT_SEC)

        assert task_id_of(polled) == "the-one-after-it"
        assert any("SomethingTheReaderHasNeverHeardOf" in record.getMessage() for record in caplog.records)

    def test_a_read_with_no_timeout_returns_immediately(self) -> None:
        _publisher, consumer = self.make_publisher_and_consumer()

        started = time.monotonic()
        assert poll(consumer) is None
        assert time.monotonic() - started < IMMEDIATE_SEC

    def test_a_read_with_a_timeout_is_bounded_by_it(self) -> None:
        _publisher, consumer = self.make_publisher_and_consumer()

        started = time.monotonic()
        assert poll(consumer, 0.3) is None
        elapsed = time.monotonic() - started

        assert 0.15 <= elapsed < 3.0

    def test_a_payload_the_wire_format_cannot_carry_is_dropped_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        publisher, consumer = self.make_publisher_and_consumer()
        if not self.serializes_events():
            pytest.skip(f"{type(self).__name__} declares that publishing encodes nothing")
        unsafe = MemoryDistillationEvent(scope=TEST_SCOPE, llm_trace={"hint": pathlib.Path("/tmp/x")})

        with caplog.at_level(logging.WARNING):
            assert publisher.publish(unsafe) is None
        publisher.publish(distillation_event("the-safe-one-after-it"))

        assert task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC)) == "the-safe-one-after-it"
        assert any(
            "MemoryDistillationEvent" in record.getMessage() and "scope=" in record.getMessage()
            for record in caplog.records
        )

    def test_a_kind_with_no_wire_form_is_dropped_at_the_publisher(self, caplog: pytest.LogCaptureFixture) -> None:
        publisher, consumer = self.make_publisher_and_consumer()
        if not self.serializes_events():
            pytest.skip(f"{type(self).__name__} declares that publishing encodes nothing")

        with caplog.at_level(logging.WARNING):
            assert publisher.publish(ConsolidationContinuationEvent(scope=TEST_SCOPE)) is None
        publisher.publish(distillation_event("the-safe-one-after-it"))

        assert task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC)) == "the-safe-one-after-it"
        assert any(
            "ConsolidationContinuationEvent" in record.getMessage() and "scope=" in record.getMessage()
            for record in caplog.records
        )

    def test_a_broker_that_comes_back_is_used_again(self) -> None:
        publisher, consumer = self.make_publisher_and_consumer()
        if not self.break_broker(publisher, consumer):
            pytest.skip(f"{type(self).__name__} declares no broker it can make unreachable")
        try:
            assert [poll(consumer, 0.05) for _ in range(5)] == [None] * 5
        finally:
            self.heal_broker(publisher, consumer)
        time.sleep(BROKER_RETRY_SEC + 0.1)

        publisher.publish(distillation_event("after-the-outage"))

        assert task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC)) == "after-the-outage"

    def test_a_poll_stops_skipping_after_a_bounded_number_of_bad_records(self) -> None:
        publisher, consumer = self.make_publisher_and_consumer()
        if not self.inject_raw_record(consumer, b"{not json"):
            pytest.skip(f"{type(self).__name__} declares that no record but a published event reaches its consumer")
        for _ in range(self.max_skipped_per_poll() + 1):
            self.inject_raw_record(consumer, b"{not json")
        publisher.publish(distillation_event("behind-the-backlog"))

        assert poll(consumer, 0.5) is None
        assert task_id_of(poll(consumer, DELIVERY_TIMEOUT_SEC)) == "behind-the-backlog"


__all__ = [
    "DELIVERY_TIMEOUT_SEC",
    "IMMEDIATE_SEC",
    "QUIET_POLL_SEC",
    "MemoryEventConformance",
    "MemoryEventPair",
    "distillation_event",
    "poll",
    "task_id_of",
]
