"""The in-process budget against the shared conformance suite, plus a ceiling only a test moves."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from new_core.coordination.inprocess import InProcessBudget
from new_core.coordination.interfaces import IConcurrencyBudget, UnknownBudget
from tests.helpers.budget_conformance import SETTLE, ConcurrencyBudgetConformance


class MutableCeilingBudget(InProcessBudget):
    """A ceiling that moves after construction: a test's need, never a runtime's."""

    def set_limit(self, budget: str, value: int) -> None:
        if int(value) < 1:
            raise ValueError("a budget must allow at least one holder")
        with self._cond:
            if budget not in self._limits:
                raise UnknownBudget(budget)
            self._limits[budget] = int(value)
            self._cond.notify_all()
            self._wake_waiters_locked()


class TestInProcessBudget(ConcurrencyBudgetConformance):
    def make_budget(self, limits: dict[str, int]) -> IConcurrencyBudget:
        return InProcessBudget(limits)


def test_a_registry_declares_its_budgets_up_front() -> None:
    with pytest.raises(ValueError):
        InProcessBudget({})
    with pytest.raises(ValueError):
        InProcessBudget({"llm": 0})


def test_a_lease_needs_a_positive_ttl() -> None:
    budget = InProcessBudget({"llm": 1})

    with pytest.raises(ValueError):
        budget.acquire("llm", holder="a", ttl_sec=0)


def test_raising_the_limit_admits_a_waiter() -> None:
    budget = MutableCeilingBudget({"llm": 1})
    assert budget.acquire("llm", holder="a", ttl_sec=60) is not None
    admitted: list[bool] = []

    def waiter() -> None:
        admitted.append(budget.acquire("llm", holder="b", ttl_sec=60, timeout_sec=5) is not None)

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(SETTLE)
    budget.set_limit("llm", 2)
    thread.join(timeout=5)

    assert admitted == [True]


def test_raising_the_limit_admits_an_awaiting_caller() -> None:
    budget = MutableCeilingBudget({"llm": 1})
    assert budget.acquire("llm", holder="a", ttl_sec=60) is not None

    async def _scenario() -> object:
        waiting = asyncio.ensure_future(budget.acquire_async("llm", holder="b", ttl_sec=60, timeout_sec=5))
        await asyncio.sleep(SETTLE)
        budget.set_limit("llm", 2)
        return await waiting

    assert asyncio.get_event_loop().run_until_complete(_scenario()) is not None


def test_lowering_the_limit_drains_rather_than_revokes() -> None:
    budget = MutableCeilingBudget({"llm": 2})
    first = budget.acquire("llm", holder="a", ttl_sec=60)
    second = budget.acquire("llm", holder="b", ttl_sec=60)
    assert first is not None and second is not None

    budget.set_limit("llm", 1)

    assert budget.in_use("llm") == 2, "a live holder is never revoked"
    budget.release(first)
    assert budget.acquire("llm", holder="c", ttl_sec=60, timeout_sec=0.05) is None
    budget.release(second)
    assert budget.acquire("llm", holder="c", ttl_sec=60, timeout_sec=0.05) is not None
