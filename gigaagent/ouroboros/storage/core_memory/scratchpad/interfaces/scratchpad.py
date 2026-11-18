"""I/O port for scratchpad block persistence.

Scratchpad is an ordered append-only buffer with no natural key, so this port is
sequence-oriented rather than keyed. It is deliberately thin: it moves blocks in
and out of a store and knows nothing about the cap, timestamp minting, or
rendering. Those rules live in
``core_memory.scratchpad.domain.service.Scratchpad``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from new_core.identity import OwnerScope


@dataclass
class BlockRow:
    """One persisted scratchpad block.

    ``id`` is assigned by the database and is the block order key; ``ts`` is
    display only and is never sorted or compared on (design D7). The block carries
    no owner of its own: which blocks a call may touch is the ``OwnerScope`` every
    method takes.
    """

    id: int
    ts: str
    source: str
    content: str
    status: str = "active"


class IScratchpad(ABC):
    """Block-level persistence for scratchpad working memory.

    Every method takes the ``OwnerScope`` of the run asking, and every statement
    filters on both ownership axes — ``owner_id`` and ``agent_id``. Two agents of one
    owner keep separate buffers, so neither evicts the other's blocks.
    """

    @abstractmethod
    def append_and_evict(
        self,
        scope: OwnerScope,
        ts: str,
        source: str,
        content: str,
        cap: int,
        *,
        job_id: str = "",
        task_id: str = "",
        claim_token: str = "",
    ) -> int:
        """Insert one active block and evict back to ``cap``, atomically. Returns the count evicted.

        One method rather than an insert and an eviction the caller sequences,
        because the pair has no useful intermediate state. Committed separately,
        a failure of the second half left the buffer permanently one block over
        its cap, and even on success every reader between the two commits saw an
        over-cap scratchpad. The store allocates the order key (design D8), so
        the insert needs nothing read back.
        """

    @abstractmethod
    def replace_blocks(
        self,
        scope: OwnerScope,
        blocks: Sequence[BlockRow],
        cap: int,
        *,
        job_id: str = "",
        task_id: str = "",
        claim_token: str = "",
    ) -> None:
        """Evict the selected active set and insert new blocks in order with fresh IDs, atomically."""

    @abstractmethod
    def list_active(self, scope: OwnerScope, *, job_id: str = "") -> list[BlockRow]:
        """Return the scope's active blocks, oldest-first by ``id``."""

    @abstractmethod
    def delete_partition(self, scope: OwnerScope, *, job_id: str = "", limit: int) -> int:
        """Delete at most ``limit`` blocks from one non-foreground partition."""


__all__ = ["BlockRow", "IScratchpad"]
