"""The single-process implementations of the coordination seams.

Correct while a deployment runs one instance, and deliberately built to the same
semantics the shared backend will have: leases expire here too. A ``BoundedSemaphore``
would be shorter and would let every caller quietly depend on a slot that is never
reclaimed — the bug would surface only on the day a second instance exists.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import NoReturn

from new_core.coordination.interfaces import IConcurrencyBudget, IWorkSignal, Lease, UnknownBudget

__all__ = ["InProcessBudget", "InProcessWorkSignal", "NullWorkSignal"]

_MAX_WAIT_SEC = 1.0


class InProcessBudget(IConcurrencyBudget):
    """Budgets held in this process's memory, keyed by name.

    Limits are supplied by the composition root rather than read from settings here: this
    package stays runtime-independent, and a budget that reads configuration is a budget
    that disagrees with the one in the next process.
    """

    def __init__(self, limits: dict[str, int]) -> None:
        if not limits:
            raise ValueError("a budget registry needs at least one declared budget")
        for name, value in limits.items():
            if int(value) < 1:
                raise ValueError(f"budget {name!r} must allow at least one holder")
        self._limits = {str(name): int(value) for name, value in limits.items()}
        #: budget -> token -> expiry (monotonic)
        self._held: dict[str, dict[str, float]] = {name: {} for name in self._limits}
        self._cond = threading.Condition()
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

    def acquire(
        self,
        budget: str,
        *,
        holder: str,
        ttl_sec: float,
        timeout_sec: float | None = None,
    ) -> Lease | None:
        if ttl_sec <= 0:
            raise ValueError("a lease needs a positive ttl")
        deadline = None if timeout_sec is None else time.monotonic() + timeout_sec
        with self._cond:
            while True:
                lease = self._grant_locked(budget, holder=holder, ttl_sec=ttl_sec)
                if lease is not None:
                    return lease
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                self._cond.wait(self._wait_for_locked(budget, deadline))

    async def acquire_async(
        self,
        budget: str,
        *,
        holder: str,
        ttl_sec: float,
        timeout_sec: float | None = None,
    ) -> Lease | None:
        if ttl_sec <= 0:
            raise ValueError("a lease needs a positive ttl")
        deadline = None if timeout_sec is None else time.monotonic() + timeout_sec
        waiter = (asyncio.get_running_loop(), asyncio.Event())
        with self._cond:
            self._waiters.append(waiter)
        try:
            while True:
                waiter[1].clear()
                with self._cond:
                    lease = self._grant_locked(budget, holder=holder, ttl_sec=ttl_sec)
                    wait_for = self._wait_for_locked(budget, deadline)
                if lease is not None:
                    return lease
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                try:
                    await asyncio.wait_for(waiter[1].wait(), wait_for)
                except asyncio.TimeoutError:
                    continue
        finally:
            with self._cond:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

    def _grant_locked(self, budget: str, *, holder: str, ttl_sec: float) -> Lease | None:
        """One attempt under the lock: prune, then grant if the ceiling allows. Never waits."""
        held = self._slots(budget)
        self._prune_locked(budget)
        if len(held) >= self._limits[budget]:
            return None
        token = uuid.uuid4().hex
        expires_at = time.monotonic() + ttl_sec
        held[token] = expires_at
        return Lease(budget=budget, holder=str(holder), token=token, expires_at=expires_at)

    def _wake_waiters_locked(self) -> None:
        for loop, event in self._waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                continue

    def release(self, lease: Lease) -> None:
        with self._cond:
            # Popped by token, so a lease that already expired and whose slot was
            # re-granted releases nothing rather than freeing the new holder's slot.
            if self._slots(lease.budget).pop(lease.token, None) is not None:
                self._cond.notify_all()
                self._wake_waiters_locked()

    def limit(self, budget: str) -> int:
        with self._cond:
            if budget not in self._limits:
                self._unknown(budget)
            return self._limits[budget]

    def in_use(self, budget: str) -> int:
        with self._cond:
            self._prune_locked(budget)
            return len(self._slots(budget))

    # -- internals --------------------------------------------------------

    def _slots(self, budget: str) -> dict[str, float]:
        slots = self._held.get(budget)
        if slots is None:
            self._unknown(budget)
        return slots

    @staticmethod
    def _unknown(budget: str) -> NoReturn:
        raise UnknownBudget(budget)

    def _prune_locked(self, budget: str) -> None:
        now = time.monotonic()
        held = self._held[budget]
        expired = [token for token, expires_at in held.items() if expires_at <= now]
        for token in expired:
            del held[token]

    def _wait_for_locked(self, budget: str, deadline: float | None) -> float:
        """How long to sleep: until the next expiry, the caller's deadline, or a cap.

        A waiter must wake for an expiry on its own — the holder that let a lease lapse is
        by definition not going to notify anyone.
        """
        now = time.monotonic()
        candidates = [_MAX_WAIT_SEC]
        held = self._held[budget]
        if held:
            candidates.append(max(0.0, min(held.values()) - now))
        if deadline is not None:
            candidates.append(max(0.0, deadline - now))
        return max(0.0, min(candidates))


class InProcessWorkSignal(IWorkSignal):
    """A wake-up backed by a ``threading.Event``: instant, and this process only.

    The event is cleared by whoever waited, so a signal raised while nobody was waiting is
    still observed by the next wait. Two signals before one wait collapse into one, which
    is correct: the waiter re-checks for work rather than counting notifications.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def signal(self) -> None:
        self._event.set()

    def wait(self, timeout: float) -> bool:
        signalled = self._event.wait(max(0.0, float(timeout)))
        if signalled:
            self._event.clear()
        return signalled


class NullWorkSignal(IWorkSignal):
    """Delivers nothing and waits out every timeout.

    What a deployment with no notification backend holds, and what a test uses to prove
    that work still starts when the signal never arrives.
    """

    def signal(self) -> None:
        return None

    def wait(self, timeout: float) -> bool:
        time.sleep(max(0.0, float(timeout)))
        return False
