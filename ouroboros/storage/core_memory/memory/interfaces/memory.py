"""I/O port for unified memory persistence.

The port is deliberately thin: it moves rows in and out of a store and knows
nothing about the kind policy, name validation, capacity, frontmatter, or index
rendering. Those rules live in
``core_memory.memory.domain.service.Memory``.

One operation reads bodies — :meth:`IMemory.fetch` — and it takes the name to
read. Bodies vary by two orders of magnitude in the same table (a fact is a
paragraph, a knowledge memory runs to 80 000 characters), so a listing that
pulled ``content`` would put that cost on every prompt build. No listing carries
a body: that is the structural guard, not a convention someone has to remember.
A caller that knows which name it needs asks for it by name.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from ouroboros.storage.core_memory.memory.domain.models import MemoryHeader


@dataclass
class MemoryRow:
    """One persisted memory, as stored (frontmatter stripped)."""

    owner_id: str
    name: str
    kind: str = "project"
    description: str = ""
    content: str = ""
    status: str = "active"
    source: str = ""
    first_recorded_at: float = 0.0
    updated_at: float = 0.0
    last_recalled_at: float = 0.0
    recall_count: int = 0


class IMemory(ABC):
    """Row-level persistence for durable named memory."""

    @abstractmethod
    def upsert(self, row: MemoryRow) -> None:
        """Insert or replace the row identified by ``(owner_id, name)``."""

    @abstractmethod
    def fetch(self, owner_id: str, name: str) -> MemoryRow | None:
        """Return one row, or None when it does not exist. Body-bearing."""

    @abstractmethod
    def delete(self, owner_id: str, name: str) -> bool:
        """Delete one row. Returns True when a row was removed."""

    @abstractmethod
    def list_headers(self, owner_id: str, *, statuses: frozenset[str] | None = None) -> list[MemoryHeader]:
        """Return headers for the owner, newest ``updated_at`` first with ``name`` breaking ties.

        ``statuses`` restricts the listing in the store; ``None`` returns every
        status, which is what the audit listing wants and nothing else does.
        Selects no ``content`` column.

        The order is part of the contract, not an accident of the query: capacity
        pruning takes its victims from the tail, so a tie broken differently by
        two adapters would archive different rows on different dialects.
        """

    @abstractmethod
    def revise(
        self,
        owner_id: str,
        name: str,
        revise: Callable[[MemoryRow | None], MemoryRow | None],
        template: MemoryRow,
    ) -> None:
        """Rewrite one memory from its current state in one transaction, creating it if absent.

        ``revise`` maps the existing row (``None`` when absent) to the row to
        persist, or to ``None`` to commit without writing. It is the one
        callable any core-memory port takes, and a deliberate exception to the
        "ports are I/O only" rule: append is a genuine read-modify-write, and
        the concatenation has to happen in Python between the read and the write
        and inside the same transaction, or two appenders lose one of their
        writes.

        ``template`` is the row a PostgreSQL adapter materializes to give its
        locking select something to lock when the name is absent. It must be the
        row the caller would create for an empty body. The materialization stays
        invisible to ``revise``: an adapter that inserts one passes ``None`` for
        the absent name exactly as SQLite does, and removes the row again if the
        callback declines to write. Both halves of the contract above therefore
        mean the same thing on both dialects.

        Passing the rules inward keeps the transaction boundary in ``infra/``,
        where dialect knowledge lives, while the separator rule and the
        provenance rules stay in ``domain/``.
        """

    @abstractmethod
    def write_and_prune(
        self,
        owner_id: str,
        kind: str,
        name: str,
        revise: Callable[[MemoryRow | None], MemoryRow | None],
        template: MemoryRow,
        prune: Callable[[list[MemoryHeader]], list[str]],
        *,
        statuses: frozenset[str],
    ) -> None:
        """Write one memory and prune its kind back to capacity in one transaction.

        This is :meth:`revise` extended with the capacity prune, so a save either
        commits its write together with its eviction or commits neither. The
        transaction is serialized by ``(owner_id, kind)``: two saves of the same
        kind — even under different names — cannot interleave their prunes, so the
        limit holds and no prune archives against a cohort another save is about
        to change. Saves of different kinds still proceed concurrently.

        ``revise`` and ``template`` mean exactly what they mean for
        :meth:`revise`: ``revise`` maps the locked existing row (``None`` when
        absent) to the row to persist, and ``template`` is the empty-body row a
        PostgreSQL adapter materializes to give its locking select something to
        lock. Both the append merge and the overwrite provenance carry-forward
        run inside ``revise``, so they read the row under the write lock.

        After the write is staged, the adapter lists the owner's headers filtered
        to ``statuses`` — body-free, in the same transaction, so the listing sees
        the row just written — and hands them to ``prune``. ``prune`` returns the
        names to archive; the capacity number and the "body-injected kinds are not
        evictable" rule live in the caller, so the port stays free of kind
        knowledge. The listing selects no ``content`` column.
        """

    @abstractmethod
    def bump_recall(self, owner_id: str, names: list[str], now: float) -> None:
        """Set ``last_recalled_at`` and increment ``recall_count`` for names."""

    @abstractmethod
    def set_status(self, owner_id: str, names: list[str], status: str) -> None:
        """Set ``status`` for the given names (used by capacity pruning)."""


__all__ = ["IMemory", "MemoryRow"]
