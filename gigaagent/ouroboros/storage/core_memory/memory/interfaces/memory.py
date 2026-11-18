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

from new_core.identity import OwnerScope
from ouroboros.storage.core_memory.memory.domain.models import MemoryHeader


@dataclass
class MemoryRow:
    """One persisted memory, as stored (frontmatter stripped).

    Carries no owner of its own: which rows a call may touch is the ``OwnerScope``
    every method takes, so a row cannot name an owner the query disagrees with.
    """

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
    """Row-level persistence for durable named memory.

    Every method takes the ``OwnerScope`` of the run asking, and every statement
    filters on both ownership axes — ``owner_id`` and ``agent_id``. Surface and chat
    ride along but never reach a predicate: a memory belongs to an agent, not to the
    conversation it happened to be learned in.
    """

    @abstractmethod
    def upsert(self, scope: OwnerScope, row: MemoryRow) -> None:
        """Insert or replace the row identified by ``(owner_id, agent_id, name)``."""

    @abstractmethod
    def fetch(self, scope: OwnerScope, name: str) -> MemoryRow | None:
        """Return one row, or None when it does not exist. Body-bearing."""

    @abstractmethod
    def delete(self, scope: OwnerScope, name: str, *, task_id: str = "", claim_token: str = "") -> bool:
        """Delete one row. Returns True when a row was removed."""

    @abstractmethod
    def list_headers(self, scope: OwnerScope, *, statuses: frozenset[str] | None = None) -> list[MemoryHeader]:
        """Return headers for the scope, newest ``updated_at`` first with ``name`` breaking ties.

        ``statuses`` restricts the listing in the store; ``None`` returns every
        status, which is what the audit listing wants and nothing else does.
        Selects no ``content`` column.

        The order is part of the contract, not an accident of the query: capacity
        pruning takes its victims from the tail, so a tie broken differently by
        two implementations would archive different rows.
        """

    @abstractmethod
    def write_and_prune(
        self,
        scope: OwnerScope,
        kind: str,
        name: str,
        revise: Callable[[MemoryRow | None], MemoryRow | None],
        prune: Callable[[list[MemoryHeader]], list[str]],
        *,
        statuses: frozenset[str],
        task_id: str = "",
        claim_token: str = "",
    ) -> None:
        """Write one memory and prune its kind back to capacity in one transaction.

        The callback receives the locked row or None. Writes and archival commit
        together under the owner/agent/kind lock. A task claim fences the transaction.
        """

    @abstractmethod
    def bump_recall(self, scope: OwnerScope, names: list[str], now: float) -> None:
        """Set ``last_recalled_at`` and increment ``recall_count`` for names."""

    @abstractmethod
    def set_status(self, scope: OwnerScope, names: list[str], status: str) -> None:
        """Set ``status`` for the given names (used by capacity pruning)."""


__all__ = ["IMemory", "MemoryRow"]
