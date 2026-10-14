"""Base for SQL adapters over the runtime DB.

Holds the engine and nothing else: every capability-specific statement lives in
that capability's own ``infra/`` module.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine


class SqlAdapterBase:
    """Carries the runtime DB engine for a capability's adapter."""

    def __init__(self, engine: Engine):
        self._engine = engine

    @property
    def engine(self) -> Engine:
        return self._engine


__all__ = ["SqlAdapterBase"]
