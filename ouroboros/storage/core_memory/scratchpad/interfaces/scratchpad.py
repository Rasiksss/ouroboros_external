"""I/O port for scratchpad block persistence.

Scratchpad is an ordered append-only buffer with no natural key, so this port is
sequence-oriented rather than keyed. It is deliberately thin: it moves blocks in
and out of a store and knows nothing about the cap, timestamp minting, or
rendering. Those rules live in
``core_memory.scratchpad.domain.service.Scratchpad``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class BlockRow:
    """One persisted scratchpad block.

    ``id`` is assigned by the database and is the block order key; ``ts`` is
    display only and is never sorted or compared on (design D7).
    """

    id: int
    owner_id: str
    ts: str
    source: str
    content: str
    status: str = "active"


class IScratchpad(ABC):
    """Block-level persistence for scratchpad working memory."""

    @abstractmethod
    def append_and_evict(self, owner_id: str, ts: str, source: str, content: str, cap: int) -> int:
        """Insert one active block and evict back to ``cap``, atomically. Returns the count evicted.

        One method rather than an insert and an eviction the caller sequences,
        because the pair has no useful intermediate state. Committed separately,
        a failure of the second half left the buffer permanently one block over
        its cap, and even on success every reader between the two commits saw an
        over-cap scratchpad. The store allocates the order key (design D8), so
        the insert needs nothing read back.
        """

    @abstractmethod
    def list_active(self, owner_id: str) -> list[BlockRow]:
        """Return the owner's active blocks, oldest-first by ``id``."""


__all__ = ["BlockRow", "IScratchpad"]
