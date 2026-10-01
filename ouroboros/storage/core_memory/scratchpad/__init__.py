"""Scratchpad working memory: domain service over a dialect-neutral I/O port.

Callers get the wired service from the module-level factory::

    from ouroboros.storage.core_memory.scratchpad import get_scratchpad

    get_scratchpad().append_block("...")

``ScratchpadFactory`` wires the service against the runtime DB of the drive root
in the current settings; ``get_scratchpad`` caches the built service for the
process. Nothing is built at import, and rebinding uses the stdlib
``cache_clear()`` rather than a test-only reset hook in business code.
"""

from __future__ import annotations

from functools import lru_cache

from ouroboros.storage.core_memory.scratchpad.domain.models import SCRATCHPAD_MAX_BLOCKS
from ouroboros.storage.core_memory.scratchpad.domain.service import Scratchpad
from ouroboros.storage.core_memory.scratchpad.interfaces.scratchpad import BlockRow, IScratchpad


class ScratchpadFactory:
    """Wires the domain service over the runtime DB.

    Holds no state beyond ``owner_id``: every call resolves the engine from the
    current settings. Callers go through ``get_scratchpad()``, which caches the
    result for the process.
    """

    def __init__(self, owner_id: str = "global"):
        self._owner_id = owner_id

    def get_scratchpad(self) -> Scratchpad:
        """Build a service bound to the runtime DB in the current settings."""
        return Scratchpad(self._build_repo(), self._owner_id)

    @classmethod
    def _build_repo(cls) -> IScratchpad:
        from ouroboros.storage.runtime_engine import resolve_runtime_engine

        # Two adapters rather than one: the append-and-evict transaction takes a
        # per-``owner_id`` serialization lock that PostgreSQL needs and SQLite
        # does not, so the lock clause is picked by dialect the way the memory
        # factory resolves its adapter.
        engine = resolve_runtime_engine("scratchpad")
        if engine.dialect.name == "postgresql":
            from ouroboros.storage.core_memory.scratchpad.infra.sql import PsqlScratchpad

            return PsqlScratchpad(engine)
        from ouroboros.storage.core_memory.scratchpad.infra.sql import SqliteScratchpad

        return SqliteScratchpad(engine)


scratchpad_factory = ScratchpadFactory()


@lru_cache
def get_scratchpad() -> Scratchpad:
    """Return the process-wide scratchpad service, built on first use.

    ``get_scratchpad.cache_clear()`` drops the built service so the next call
    rebinds to the current settings — that is how tests get an isolated DB.
    """
    return scratchpad_factory.get_scratchpad()


__all__ = [
    "SCRATCHPAD_MAX_BLOCKS",
    "BlockRow",
    "IScratchpad",
    "Scratchpad",
    "ScratchpadFactory",
    "get_scratchpad",
    "scratchpad_factory",
]
