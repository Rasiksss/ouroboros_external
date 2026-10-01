"""Runtime DB engine resolution shared by the subsystems that store rows beside the runtime store."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def resolve_runtime_engine(capability: str) -> Engine:
    """Return the runtime DB engine for the drive root in the current settings.

    The engine is shared with the runtime store, which serializes its own
    statements with a process-local lock these adapters do not take: isolation
    here is the database's, not that lock's. That is why the SQLite engine is
    built with a busy timeout — see ``sql_runtime_store._register_sqlite_pragmas``.

    ``capability`` only names the caller in the error raised when the resolved
    runtime store has no SQL engine to hand out.
    """
    from ouroboros.storage.runtime_store import get_runtime_store
    from ouroboros.storage.settings.paths import load_path_settings

    paths = load_path_settings()
    store = get_runtime_store(paths.drive_root or paths.data_dir)
    # ``RuntimeStore`` declares ``engine``; the guard is for a store that is
    # absent (no drive root resolved yet) or a stand-in that leaves it unset.
    engine = getattr(store, "engine", None)
    if engine is None:
        raise RuntimeError(f"runtime store {type(store).__name__} exposes no SQL engine for {capability}")
    return engine


__all__ = ["resolve_runtime_engine"]
