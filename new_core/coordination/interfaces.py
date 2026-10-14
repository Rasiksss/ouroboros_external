"""Coordination seams shared by every runner: budgets, wake-ups, and stop hints.

A deployment runs several instances, each running several agents, and some resources
are bounded for the *deployment* rather than for a process: how many model calls may be
in flight against one provider, how many tool bodies may run at once. A process-local
semaphore answers that question wrongly the moment there are two processes, so the
count lives behind this seam and moves to a shared backend without any caller changing.

**This is admission control, never mutual exclusion.** A budget answers "may one more
start" approximately; it does not guarantee that exactly N are running. Nothing whose
correctness depends on a count may be built on it — task claiming, terminal-status
transitions and owner state are correctness, and they belong in the database, which
settles them in a transaction. If a future caller reaches for a budget as a lock, the
shared implementation will break it silently, which is why the limit is stated here
rather than left to be discovered.

The shape is a lease, not a counter, because the remote implementation has a failure
mode the local one does not: a holder can die, and its slot has to come back without
anyone releasing it. Designing to the local implementation's failure mode — none — is
how a seam passes its tests in-process and deadlocks on the first dead pod. The
in-process implementation therefore expires leases too, so both sides of the seam share
one set of semantics and one conformance suite.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

__all__ = [
    "BudgetTimeout",
    "IConcurrencyBudget",
    "ITaskStopConsumer",
    "ITaskStopPublisher",
    "IWorkSignal",
    "Lease",
    "TaskStopNotice",
    "UnknownBudget",
]


class UnknownBudget(KeyError):
    """A budget name nobody declared. Budgets are declared, never created on demand."""


class BudgetTimeout(TimeoutError):
    """No slot became free before the caller's deadline."""


@dataclass(frozen=True)
class Lease:
    """One grant: which budget, who holds it, and which grant this is.

    ``token`` is a fencing token, fresh per grant. A holder whose lease expired and whose
    slot was re-granted must not be able to release the new holder's slot, and the token
    is what tells the two apart — a backend that releases by decrementing a counter gets
    this wrong and over-admits forever after.

    ``expires_at`` is advisory. Whether a lease is still held is a question for the budget
    that granted it, never arithmetic on this field: the clock that decides belongs to the
    implementation, and on a shared backend it is not this process's clock.
    """

    budget: str
    holder: str
    token: str
    expires_at: float


class IConcurrencyBudget(ABC):
    """A named, counted admission budget with leased grants."""

    @abstractmethod
    def acquire(
        self,
        budget: str,
        *,
        holder: str,
        ttl_sec: float,
        timeout_sec: float | None = None,
    ) -> Lease | None:
        """Take one slot, or return ``None`` when ``timeout_sec`` passes first.

        ``ttl_sec`` is how long the slot is held if the holder never releases it, and a
        grant is never extended: there is no renewal, so the ttl is the whole of what the
        budget knows about how long the work may take. Size it above the work's own
        deadline — a lease that expires under a live holder over-admits the budget.
        ``timeout_sec=None`` waits indefinitely; a caller that cannot afford to is the
        caller that must say so.
        """

    @abstractmethod
    async def acquire_async(
        self,
        budget: str,
        *,
        holder: str,
        ttl_sec: float,
        timeout_sec: float | None = None,
    ) -> Lease | None:
        """:meth:`acquire` for a caller on an event loop, waiting without occupying it.

        Abstract rather than a default over the blocking form: a caller that waited through
        :meth:`acquire` stalls every other piece of work sharing its loop, including work
        holding nothing, and a default that happened to satisfy one implementation would put
        that stall one inherited method away from every future one. An implementation that
        waits by moving the blocking form to a thread reintroduces a thread per waiting
        caller, which is what an awaitable acquisition exists to remove.

        The same semantics as :meth:`acquire` otherwise: the lease, the fencing token, the
        ttl that is never extended, and ``timeout_sec=None`` waiting indefinitely.
        """

    @abstractmethod
    def release(self, lease: Lease) -> None:
        """Give the slot back. Idempotent, and a no-op for a lease already lost.

        Releasing twice, or releasing after expiry, must not credit a slot that now
        belongs to someone else.
        """

    @abstractmethod
    def limit(self, budget: str) -> int:
        """The budget's current ceiling. Raises :class:`UnknownBudget` for a name nobody declared."""

    @abstractmethod
    def in_use(self, budget: str) -> int:
        """Slots currently held. Observability only — approximate on a shared backend."""

    @contextmanager
    def slot(
        self,
        budget: str,
        *,
        holder: str,
        ttl_sec: float,
        timeout_sec: float | None = None,
    ) -> Iterator[Lease]:
        """Hold one slot for the block, releasing it on every exit path."""
        lease = self.acquire(budget, holder=holder, ttl_sec=ttl_sec, timeout_sec=timeout_sec)
        if lease is None:
            raise BudgetTimeout(f"no slot in budget {budget!r} within {timeout_sec}s")
        try:
            yield lease
        finally:
            self.release(lease)

    @asynccontextmanager
    async def async_slot(
        self,
        budget: str,
        *,
        holder: str,
        ttl_sec: float,
        timeout_sec: float | None = None,
    ) -> AsyncIterator[Lease]:
        """:meth:`slot` for an awaiting caller. Concrete for the reason :meth:`slot` is."""
        lease = await self.acquire_async(budget, holder=holder, ttl_sec=ttl_sec, timeout_sec=timeout_sec)
        if lease is None:
            raise BudgetTimeout(f"no slot in budget {budget!r} within {timeout_sec}s")
        try:
            yield lease
        finally:
            self.release(lease)


class IWorkSignal(ABC):
    """A hint that work may be available, and a bounded wait for one.

    An optimisation and never a guarantee. A runner polls for its own work on an interval
    and that poll is sufficient on its own; a signal only shortens the wait. Losing one,
    failing to deliver one, or having no backend at all SHALL cost latency and SHALL NOT
    strand claimable work — which is what lets the in-process implementation be an event
    today and a shared backend later without any caller changing.

    That rule is the same one the budget above states for leases, for the same reason:
    designing to the local implementation's failure mode is how a seam passes its tests
    in-process and strands work on the first dead pod.
    """

    @abstractmethod
    def signal(self) -> None:
        """Say that work may be available now. Best effort, and never blocks the caller."""

    @abstractmethod
    def wait(self, timeout: float) -> bool:
        """Wait at most ``timeout`` seconds for a signal; report whether one arrived.

        A caller may ignore the answer entirely — it re-checks either way.
        """


@dataclass(frozen=True)
class TaskStopNotice:
    """A best-effort hint that named tasks have durable stop intent."""

    runtime_id: str
    task_ids: tuple[str, ...]
    version: int = 1


class ITaskStopPublisher(ABC):
    """The producer half of the replaceable task-stop notification seam."""

    @abstractmethod
    def publish(self, notice: TaskStopNotice) -> None:
        """Publish one hint; durable correctness remains in the task store."""


class ITaskStopConsumer(ABC):
    """The consumer half of the replaceable task-stop notification seam."""

    @abstractmethod
    def start(self) -> bool:
        """Prepare before claims; return false only with replay-safe recovery.

        A controlled broker outage may leave the fast path unavailable while durable store polling
        keeps execution correct. In that case a later poll must resume before every notice that
        could have been published after this call, rather than anchoring at a newer tail.
        """

    @abstractmethod
    def poll(self, timeout: float = 0.0) -> TaskStopNotice | None:
        """Return the next hint, or ``None`` within the bounded timeout."""
