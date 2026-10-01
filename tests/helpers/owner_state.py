"""Test helpers for DB-backed owner runtime state."""

from __future__ import annotations

import pathlib
from typing import Any

from ouroboros.storage.owner_state import ensure_state_defaults
from ouroboros.storage.runtime_models import OwnerRuntimeState
from ouroboros.storage.runtime_store import init_runtime_store
from supervisor.state import RuntimeStateRepository


def seed_owner_state(drive_root: pathlib.Path | str, **overrides: Any) -> RuntimeStateRepository:
    """Persist owner runtime state in the metadata DB for tests."""
    root = pathlib.Path(drive_root)
    store = init_runtime_store(root)
    repo = RuntimeStateRepository(root, store=store)
    payload = ensure_state_defaults(dict(overrides))
    repo.save(OwnerRuntimeState.model_validate(payload))
    return repo
