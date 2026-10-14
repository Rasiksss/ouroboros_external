"""Awaiting the seam is the loop's idle state: it yields, and it holds no thread."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from background_memory.infra.events.inmemory import InMemoryEventConsumer, InMemoryEventPublisher, MemoryEventQueue
from background_memory.infra.events.outage import AsyncBrokerOutage, wait_out
from tests.helpers.memory_event_conformance import distillation_event


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# --- the in-process transport -------------------------------------------------


def test_an_awaited_poll_on_an_empty_queue_lets_its_neighbours_run() -> None:
    """A `queue.Queue.get` here would stop every coroutine on the loop for the timeout."""
    consumer = InMemoryEventConsumer(MemoryEventQueue(4))
    ticks: list[int] = []

    async def _scenario():
        waiting = asyncio.ensure_future(consumer.poll(0.5))
        for index in range(5):
            ticks.append(index)
            await asyncio.sleep(0.01)
        return await waiting

    assert _run(_scenario()) is None
    assert ticks == [0, 1, 2, 3, 4]


def test_an_awaited_poll_starts_no_thread() -> None:
    """The obvious port — read the queue on an executor — is a thread per poll."""
    consumer = InMemoryEventConsumer(MemoryEventQueue(4))
    before = threading.active_count()

    assert _run(consumer.poll(0.2)) is None

    assert threading.active_count() <= before


def test_a_put_from_another_thread_wakes_the_waiting_poll() -> None:
    """The publisher is synchronous and in a thread; the consumer waits on the loop."""
    queue = MemoryEventQueue(4)
    publisher, consumer = InMemoryEventPublisher(queue), InMemoryEventConsumer(queue)
    event = distillation_event("published-from-a-thread")

    async def _scenario():
        waiting = asyncio.ensure_future(consumer.poll(5.0))
        await asyncio.sleep(0.02)
        thread = threading.Thread(target=publisher.publish, args=(event,), daemon=True)
        thread.start()
        polled = await waiting
        thread.join(timeout=5)
        return polled

    assert _run(_scenario()) == event


def test_the_publishing_half_still_drops_the_newest_when_the_queue_is_full(caplog) -> None:
    queue = MemoryEventQueue(1)
    publisher = InMemoryEventPublisher(queue)

    publisher.publish(distillation_event("kept"))
    with caplog.at_level(logging.WARNING):
        publisher.publish(distillation_event("dropped"))

    assert any("queue full (cap=1)" in record.getMessage() for record in caplog.records)
    assert _run(InMemoryEventConsumer(queue).poll()).task.task_id == "kept"


# --- the idle policy ----------------------------------------------------------


def test_the_policy_decides_without_waiting(monkeypatch) -> None:
    """`stands` and `record` return; the caller is the one that spends the time."""
    import time as time_mod

    slept: list[float] = []
    monkeypatch.setattr(time_mod, "sleep", lambda seconds: slept.append(seconds))
    outage = AsyncBrokerOutage(
        logger=logging.getLogger("tests.async_outage"), retry_after_sec=60.0, report_every_sec=60.0
    )

    outage.record("the broker is not there")

    assert (outage.stands(), slept) == (True, [])


def test_waiting_out_an_outage_does_not_occupy_the_loop() -> None:
    ticks: list[int] = []

    async def _scenario():
        waiting = asyncio.ensure_future(wait_out(asyncio.get_running_loop().time() + 0.0))
        held = asyncio.ensure_future(wait_out(_deadline_in(0.1)))
        for index in range(5):
            ticks.append(index)
            await asyncio.sleep(0.01)
        await waiting
        await held

    _run(_scenario())

    assert ticks == [0, 1, 2, 3, 4]


def _deadline_in(seconds: float) -> float:
    import time

    return time.monotonic() + seconds


def test_an_ongoing_outage_is_reported_periodically_rather_than_once_per_attempt(caplog) -> None:
    outage = AsyncBrokerOutage(
        logger=logging.getLogger("tests.async_outage"), retry_after_sec=0.0, report_every_sec=60.0
    )

    with caplog.at_level(logging.WARNING, logger="tests.async_outage"):
        for _ in range(10):
            outage.record("the broker is not there")

    assert len(caplog.records) == 1


def test_a_broker_that_comes_back_clears_the_outage_and_says_so(caplog) -> None:
    outage = AsyncBrokerOutage(
        logger=logging.getLogger("tests.async_outage"), retry_after_sec=0.0, report_every_sec=60.0
    )
    outage.record("the broker is not there")
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="tests.async_outage"):
        outage.cleared("the broker answered again")
        outage.cleared("the broker answered again")

    assert [record.getMessage() for record in caplog.records] == ["the broker answered again"]
    assert not outage.stands()


def test_the_shared_synchronous_policy_and_its_call_sites_are_untouched() -> None:
    """Seven instantiations in four modules this series does not change."""
    import inspect
    import pathlib

    from ouroboros.contracts.stream_replies import BrokerOutage

    source = inspect.getsource(BrokerOutage)
    assert "time.sleep" not in source, "the shared policy's waiting is `wait_out`, which sleeps"
    assert "wait_out(deadline)" in source, "the shared policy still ends its decisions in a wait"

    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    shared = re.compile(r"(?<!Async)BrokerOutage\(")
    users = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
        and not str(path.relative_to(root)).startswith("tests")
        and shared.search(path.read_text(encoding="utf-8"))
    )

    # `background_memory/infra/events/kafka_transport.py` left this list with
    # `read-memory-events-with-aiokafka`: its reader is asynchronous end to end now, so it
    # applies the package's own policy like its Redis sibling.
    assert users == [
        "ouroboros/agent_events/kafka_transport.py",
        "ouroboros/agent_events/redis_transport.py",
        "ouroboros/app/task_stop_kafka.py",
        "transport/infra/kafka_gateway.py",
        "transport/infra/redis_gateway.py",
    ], users


@pytest.mark.parametrize("timeout", [0.0, 0.05])
def test_an_awaited_poll_returns_nothing_rather_than_raising_on_an_empty_queue(timeout: float) -> None:
    assert _run(InMemoryEventConsumer(MemoryEventQueue(2)).poll(timeout)) is None
