#!/usr/bin/env python3
"""Enumerate memory-name collisions before the ``0006_core_memory`` migration runs.

The unified ``memory`` table keys on ``(owner_id, name)`` across every kind, so a
long-term fact and a knowledge topic that share a name are a genuine conflict.
The migration aborts on one rather than renaming a side, because a silently
renamed memory is unaddressable by the name the agent knows.

Run this against the deployment *before* merging, resolve any conflict by
renaming one side, then re-run. Run it with the previous version drained: the
migration requires that no legacy writer is serving traffic, and this report
carries the same requirement — a name written after the read is in neither::

    OUROBOROS_STORAGE_BACKEND=s3 OUROBOROS_S3_BUCKET=... python scripts/memory_name_collisions.py

Exits 0 when no collision exists, 1 when at least one does. Reads through the
migration's own frozen readers so this reports exactly what the migration will
see, not what a later refactoring of the storage layer would report.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys


def _load_migration_module():
    """Import ``0006_core_memory`` by path — its name is not a valid identifier."""
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "ouroboros"
        / "storage"
        / "migrations"
        / "versions"
        / "0006_core_memory.py"
    )
    spec = importlib.util.spec_from_file_location("_core_memory_migration", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the migration module at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    migration = _load_migration_module()
    reader = migration._blob_reader()

    # Same snapshot the migration takes, and the same check over it. Only
    # meaningful against a quiesced deployment: a name written after this read
    # is not in the snapshot, so it is neither reported here nor copied there.
    collisions = migration.collect_name_collisions(migration.collect_source_rows(reader))
    if not collisions:
        print("No memory-name collisions. The migration will not abort on this.")
        return 0

    print(f"{len(collisions)} memory-name collision(s) — the migration will abort:\n")
    for name, sources in sorted(collisions.items()):
        print(f"  {name}: claimed by {', '.join(sources)}")
    print("\nRename one side of each pair on the storage backend, then re-run.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
