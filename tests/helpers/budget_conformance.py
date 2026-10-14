"""Conformance tests for ``new_core.coordination.interfaces.IConcurrencyBudget``.

Written against the interface, not an implementation: every semantic that only matters
for a shared backend — a lease that expires under a dead holder, a release that must not
credit someone else's slot — would otherwise be untested by construction, because the in-process budget is the only one that ships today.
A Redis-backed budget is expected to pass this same suite unchanged.

To run it against an implementation, subclass and implement the factory::

    class TestMyBudget(ConcurrencyBudgetConformance):
        def make_budget(self, limits):
            return MyBudget(limits)

The class name deliberately lacks a ``Test`` prefix so pytest collects the cases only
through a subclass that can build the thing under test.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from new_core.coordination.interfaces import BudgetTimeout, IConcurrencyBudget, UnknownBudget

#: Short enough to keep the suite quick, long enough that a loaded machine does not
#: expire a lease the test still considers live.
TTL = 0.4
SETTLE = 0.15


def _run(coro):
    """The tree's async-test form: one loop per test, installed by ``tests/conftest.py``."""
    return asyncio.get_event_loop().run_until_complete(coro)


class ConcurrencyBudgetConformance:
    """Semantics every ``IConcurrencyBudget`` must hold, local or shared."""

    def make_budget(self, limits: dict[str, int]) -> IConcurrencyBudget:
        raise NotImplementedError

    # -- counting ---------------------------------------------------------

    def test_a_budget_admits_up_to_its_limit_and_no_further(self) -> None:
        budget = self.make_budget({"llm": 2})

        first = budget.acquire("llm", holder="a", ttl_sec=60)
        second = budget.acquire("llm", holder="b", ttl_sec=60)
        third = budget.acquire("llm", holder="c", ttl_sec=60, timeout_sec=0.05)

        assert first is not None and second is not None
        assert third is None
        assert budget.in_use("llm") == 2

    def test_releasing_admits_the_next_waiter(self) -> None:
        budget = self.make_budget({"llm": 1})
        held = budget.acquire("llm", holder="a", ttl_sec=60)
        assert held is not None
        admitted: list[bool] = []

        def waiter() -> None:
            lease = budget.acquire("llm", holder="b", ttl_sec=60, timeout_sec=5)
            admitted.append(lease is not None)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(SETTLE)
        assert admitted == [], "the waiter must not be admitted while the slot is held"
        budget.release(held)
        thread.join(timeout=5)

        assert admitted == [True]

    def test_concurrent_callers_never_exceed_the_limit(self) -> None:
        budget = self.make_budget({"llm": 3})
        peak = 0
        live = 0
        guard = threading.Lock()
        start = threading.Barrier(12)

        def worker() -> None:
            nonlocal peak, live
            start.wait()
            with budget.slot("llm", holder="w", ttl_sec=60, timeout_sec=10):
                with guard:
                    live += 1
                    peak = max(peak, live)
                time.sleep(0.02)
                with guard:
                    live -= 1

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert peak <= 3
        assert budget.in_use("llm") == 0

    # -- leases -----------------------------------------------------------

    def test_an_expired_lease_frees_its_slot_without_a_release(self) -> None:
        """The dead-holder case: nobody releases, and the budget must recover anyway."""
        budget = self.make_budget({"llm": 1})
        assert budget.acquire("llm", holder="dead", ttl_sec=TTL) is not None

        recovered = budget.acquire("llm", holder="next", ttl_sec=60, timeout_sec=TTL * 6)

        assert recovered is not None

    def test_releasing_an_expired_lease_does_not_free_the_new_holder(self) -> None:
        """Fencing: a backend that releases by decrementing a counter fails here."""
        budget = self.make_budget({"llm": 1})
        stale = budget.acquire("llm", holder="dead", ttl_sec=TTL)
        assert stale is not None
        successor = budget.acquire("llm", holder="next", ttl_sec=60, timeout_sec=TTL * 6)
        assert successor is not None

        budget.release(stale)

        assert budget.in_use("llm") == 1
        assert budget.acquire("llm", holder="c", ttl_sec=60, timeout_sec=0.05) is None

    def test_release_is_idempotent(self) -> None:
        budget = self.make_budget({"llm": 1})
        lease = budget.acquire("llm", holder="a", ttl_sec=60)
        assert lease is not None

        budget.release(lease)
        budget.release(lease)

        assert budget.in_use("llm") == 0
        assert budget.acquire("llm", holder="b", ttl_sec=60, timeout_sec=0.05) is not None
        assert budget.acquire("llm", holder="c", ttl_sec=60, timeout_sec=0.05) is None

    # -- declaration and ergonomics --------------------------------------

    def test_an_undeclared_budget_raises(self) -> None:
        budget = self.make_budget({"llm": 1})

        with pytest.raises(UnknownBudget):
            budget.limit("nobody-declared-this")

    def test_the_slot_helper_releases_on_an_exception(self) -> None:
        budget = self.make_budget({"llm": 1})

        with pytest.raises(RuntimeError):
            with budget.slot("llm", holder="a", ttl_sec=60, timeout_sec=1):
                raise RuntimeError("body failed")

        assert budget.in_use("llm") == 0

    def test_the_slot_helper_reports_a_timeout(self) -> None:
        budget = self.make_budget({"llm": 1})
        assert budget.acquire("llm", holder="a", ttl_sec=60) is not None

        with pytest.raises(BudgetTimeout):
            with budget.slot("llm", holder="b", ttl_sec=60, timeout_sec=0.05):
                pass

    # -- the awaitable half -----------------------------------------------

    def test_an_awaited_acquisition_grants_and_reports_its_timeout(self) -> None:
        budget = self.make_budget({"llm": 1})

        async def _scenario() -> tuple[object, object]:
            first = await budget.acquire_async("llm", holder="a", ttl_sec=60)
            second = await budget.acquire_async("llm", holder="b", ttl_sec=60, timeout_sec=0.05)
            return first, second

        first, second = _run(_scenario())

        assert first is not None and second is None
        assert budget.in_use("llm") == 1

    def test_an_awaiting_caller_lets_its_neighbours_run(self) -> None:
        """The whole point of the awaitable form: the loop is not the waiter's to hold."""
        budget = self.make_budget({"llm": 1})
        held = budget.acquire("llm", holder="a", ttl_sec=60)
        assert held is not None
        ticks: list[int] = []

        async def _scenario() -> object:
            async def neighbour() -> None:
                for index in range(5):
                    ticks.append(index)
                    await asyncio.sleep(0.01)
                budget.release(held)

            waiting = asyncio.ensure_future(budget.acquire_async("llm", holder="b", ttl_sec=60, timeout_sec=5))
            await neighbour()
            return await waiting

        lease = _run(_scenario())

        assert ticks == [0, 1, 2, 3, 4], "the neighbour never ran while the caller waited"
        assert lease is not None

    def test_two_awaiting_holders_on_one_loop_are_two_holders(self) -> None:
        budget = self.make_budget({"llm": 2})

        async def _scenario() -> tuple[object, object]:
            return await asyncio.gather(
                budget.acquire_async("llm", holder="first", ttl_sec=60),
                budget.acquire_async("llm", holder="second", ttl_sec=60),
            )

        first, second = _run(_scenario())

        assert first is not None and second is not None
        assert first.token != second.token
        budget.release(first)
        assert budget.in_use("llm") == 1, "releasing one holder must not free the other"

    def test_an_awaiting_caller_recovers_a_lease_nobody_released(self) -> None:
        budget = self.make_budget({"llm": 1})
        assert budget.acquire("llm", holder="dead", ttl_sec=TTL) is not None

        recovered = _run(budget.acquire_async("llm", holder="next", ttl_sec=60, timeout_sec=TTL * 6))

        assert recovered is not None

    def test_the_async_slot_helper_releases_on_an_exception(self) -> None:
        budget = self.make_budget({"llm": 1})

        async def _scenario() -> None:
            async with budget.async_slot("llm", holder="a", ttl_sec=60, timeout_sec=1):
                raise RuntimeError("body failed")

        with pytest.raises(RuntimeError):
            _run(_scenario())

        assert budget.in_use("llm") == 0

    def test_the_async_slot_helper_reports_a_timeout(self) -> None:
        budget = self.make_budget({"llm": 1})
        assert budget.acquire("llm", holder="a", ttl_sec=60) is not None

        async def _scenario() -> None:
            async with budget.async_slot("llm", holder="b", ttl_sec=60, timeout_sec=0.05):
                pass

        with pytest.raises(BudgetTimeout):
            _run(_scenario())
