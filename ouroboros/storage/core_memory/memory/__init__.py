"""Unified durable memory: domain service over a dialect-specific I/O port.

One table, one service, one port, one tool surface for every durable named
memory — identity, long-term facts, and operational knowledge alike. What used
to distinguish them is now a ``kind`` with a policy record behind it (see
``domain/models.py``), not a separate subsystem with its own retrieval path.

Callers get the wired service from the module-level factory::

    from ouroboros.storage.core_memory.memory import get_memory

    get_memory().save("git-recipes", "knowledge", "Rebase workflow", "...")

``MemoryFactory`` wires the service against the runtime DB of the drive root in
the current settings; ``get_memory`` caches the built service for the process.
Nothing is built at import, and rebinding uses the stdlib ``cache_clear()``
rather than a test-only reset hook in business code.
"""

from __future__ import annotations

from functools import lru_cache

from ouroboros.storage.core_memory.memory.domain.errors import MemoryNotFoundError
from ouroboros.storage.core_memory.memory.domain.models import (
    DEFAULT_IDENTITY,
    IDENTITY_NAME,
    KIND_POLICIES,
    KINDS,
    WRITE_MODES,
    KindPolicy,
    MemoryHeader,
    allowed_kinds,
    effective_injection,
    identity_self_edit_enabled,
    kinds_with_injection,
    policy_for,
    writable_kinds,
)
from ouroboros.storage.core_memory.memory.domain.render import IndexSection, validate_name
from ouroboros.storage.core_memory.memory.domain.service import Memory
from ouroboros.storage.core_memory.memory.interfaces.memory import IMemory, MemoryRow


class MemoryFactory:
    """Wires the domain service over the runtime DB.

    Holds no state beyond ``owner_id``: every call resolves the engine from the
    current settings and picks the dialect adapter. Callers go through
    ``get_memory()``, which caches the result for the process.
    """

    def __init__(self, owner_id: str = "global"):
        self._owner_id = owner_id

    def get_memory(self) -> Memory:
        """Build a service bound to the runtime DB in the current settings."""
        return Memory(self._build_repo(), self._owner_id)

    @classmethod
    def _build_repo(cls) -> IMemory:
        from ouroboros.storage.runtime_engine import resolve_runtime_engine

        # Two adapters rather than one: the upsert's conflict-clause casing and
        # the way the append transaction opens and locks genuinely differ.
        engine = resolve_runtime_engine("memory")
        if engine.dialect.name == "postgresql":
            from ouroboros.storage.core_memory.memory.infra.psql import PsqlMemory

            return PsqlMemory(engine)
        from ouroboros.storage.core_memory.memory.infra.sqlite import SqliteMemory

        return SqliteMemory(engine)


memory_factory = MemoryFactory()


@lru_cache
def get_memory() -> Memory:
    """Return the process-wide memory service, built on first use.

    ``get_memory.cache_clear()`` drops the built service so the next call
    rebinds to the current settings — that is how tests get an isolated DB.
    """
    return memory_factory.get_memory()


__all__ = [
    "DEFAULT_IDENTITY",
    "IDENTITY_NAME",
    "KINDS",
    "KIND_POLICIES",
    "WRITE_MODES",
    "IMemory",
    "IndexSection",
    "KindPolicy",
    "Memory",
    "MemoryFactory",
    "MemoryHeader",
    "MemoryNotFoundError",
    "MemoryRow",
    "allowed_kinds",
    "effective_injection",
    "get_memory",
    "identity_self_edit_enabled",
    "kinds_with_injection",
    "memory_factory",
    "policy_for",
    "validate_name",
    "writable_kinds",
]
