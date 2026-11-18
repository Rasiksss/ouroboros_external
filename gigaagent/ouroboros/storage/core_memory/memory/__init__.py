"""Unified durable memory: domain service over one I/O port.

One table, one service, one port, one tool surface for every durable named
memory — long-term facts and operational knowledge. What used
to distinguish them is now a ``kind`` with a policy record behind it (see
``domain/models.py``), not a separate subsystem with its own retrieval path.

Callers get the wired service from the module-level factory::

    from ouroboros.storage.core_memory.memory import get_memory

    get_memory().save(ctx.scope, "git-recipes", "knowledge", "Rebase workflow", "...")

``MemoryFactory`` wires the service against the configured runtime database;
``get_memory`` caches the built service for the process.
Nothing is built at import, and rebinding uses the stdlib ``cache_clear()``
rather than a test-only reset hook in business code.
"""

from __future__ import annotations

from functools import lru_cache

from ouroboros.storage.core_memory.memory.domain.errors import MemoryConflictError, MemoryNotFoundError
from ouroboros.storage.core_memory.memory.domain.models import (
    KIND_POLICIES,
    KINDS,
    WRITE_MODES,
    KindPolicy,
    MemoryHeader,
    allowed_kinds,
    kinds_with_injection,
    policy_for,
)
from ouroboros.storage.core_memory.memory.domain.render import IndexSection, validate_name
from ouroboros.storage.core_memory.memory.domain.service import Memory
from ouroboros.storage.core_memory.memory.interfaces.memory import IMemory, MemoryRow


class MemoryFactory:
    """Wires the domain service over the runtime DB.

    Holds no state at all: every call resolves the engine from the current settings,
    and the service it builds takes its identity from the ``OwnerScope`` each of its
    methods is given. Callers go through ``get_memory()``, which caches the result for
    the process.
    """

    def get_memory(self) -> Memory:
        """Build a service bound to the runtime DB in the current settings."""
        return Memory(self._build_repo())

    @classmethod
    def _build_repo(cls) -> IMemory:
        from ouroboros.storage.core_memory.memory.infra.psql import PsqlMemory
        from ouroboros.storage.runtime_engine import resolve_runtime_engine

        return PsqlMemory(resolve_runtime_engine("memory"))


memory_factory = MemoryFactory()


@lru_cache
def get_memory() -> Memory:
    """Return the process-wide memory service, built on first use.

    ``get_memory.cache_clear()`` drops the built service so the next call
    rebinds to the current settings — that is how tests get an isolated DB.
    """
    return memory_factory.get_memory()


__all__ = [
    "KINDS",
    "KIND_POLICIES",
    "WRITE_MODES",
    "IMemory",
    "IndexSection",
    "KindPolicy",
    "Memory",
    "MemoryConflictError",
    "MemoryFactory",
    "MemoryHeader",
    "MemoryNotFoundError",
    "MemoryRow",
    "allowed_kinds",
    "get_memory",
    "kinds_with_injection",
    "memory_factory",
    "policy_for",
    "validate_name",
]
