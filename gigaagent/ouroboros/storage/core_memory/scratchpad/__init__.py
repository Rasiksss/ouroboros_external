"""Scratchpad working memory: domain service over one I/O port.

Callers get the wired service from the module-level factory::

    from ouroboros.storage.core_memory.scratchpad import get_scratchpad

    get_scratchpad().append_block(ctx.scope, "...")

``get_scratchpad`` wires the service against the configured runtime database
and caches the built service for the process. Nothing is built at import, and rebinding uses the stdlib
``cache_clear()`` rather than a test-only reset hook in business code.
"""

from __future__ import annotations

from functools import lru_cache

from ouroboros.storage.core_memory.scratchpad.domain.models import SCRATCHPAD_MAX_BLOCKS
from ouroboros.storage.core_memory.scratchpad.domain.service import Scratchpad
from ouroboros.storage.core_memory.scratchpad.interfaces.scratchpad import BlockRow, IScratchpad


@lru_cache
def get_scratchpad() -> Scratchpad:
    """Return the process-wide scratchpad service, built on first use.

    ``get_scratchpad.cache_clear()`` drops the built service so the next call
    rebinds to the current settings — that is how tests get an isolated DB.
    """
    from ouroboros.storage.core_memory.scratchpad.infra.sql import SqlScratchpad
    from ouroboros.storage.runtime_engine import resolve_runtime_engine

    return Scratchpad(SqlScratchpad(resolve_runtime_engine("scratchpad")))


__all__ = [
    "SCRATCHPAD_MAX_BLOCKS",
    "BlockRow",
    "IScratchpad",
    "Scratchpad",
    "get_scratchpad",
]
