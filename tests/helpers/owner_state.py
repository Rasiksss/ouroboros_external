"""Test helpers for DB-backed owner runtime state."""

from __future__ import annotations

import pathlib
from typing import Any

from new_core.identity import OwnerScope
from ouroboros.storage.owner_state import ensure_state_defaults
from ouroboros.storage.runtime_models import OwnerRuntimeState
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.state import RuntimeStateRepository, configured_owner_state_scope
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.usage import sql_usage_repository


def seed_owner_state(
    drive_root: pathlib.Path | str, *, scope: OwnerScope | None = None, **overrides: Any
) -> RuntimeStateRepository:
    """Persist one owner's runtime state in the metadata DB for tests.

    The row is per owner now, so the seed says whose it is. Without a scope it seeds the
    instance's configured owner — the same widening a caller with no owner in hand uses.
    """
    root = pathlib.Path(drive_root)
    store = runtime_persistence.initialize()
    repo = RuntimeStateRepository(
        root,
        owner_state_repository=sql_owner_state_repository(store.engine),
        scope=scope or configured_owner_state_scope(),
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    payload = ensure_state_defaults(dict(overrides))
    repo.save(OwnerRuntimeState.model_validate(payload))
    return repo
