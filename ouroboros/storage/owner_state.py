"""Owner runtime state helpers (DB-backed)."""

from __future__ import annotations

import pathlib
import uuid
from typing import Any

from ouroboros.storage.runtime_models import OwnerRuntimeState
from ouroboros.storage.runtime_store import get_runtime_store, init_runtime_store

OWNER_STATE_PROMPT_KEYS: frozenset[str] = frozenset(
    {
        "owner_id",
        "owner_chat_id",
        "owner_chat_source",
        "session_id",
        "spent_calls",
        "spent_tokens_prompt",
        "spent_tokens_completion",
        "spent_tokens_cached",
        "last_owner_message_at",
        "provider_state",
    }
)


def _coerce_owner_state(st: dict[str, Any]) -> OwnerRuntimeState:
    payload = dict(st)
    if not str(payload.get("session_id") or "").strip():
        payload["session_id"] = uuid.uuid4().hex
    return OwnerRuntimeState.model_validate(payload)


def ensure_state_defaults(st: dict[str, Any]) -> dict[str, Any]:
    """Normalize dict to the owner-state schema (unknown keys discarded)."""
    return _coerce_owner_state(st).model_dump(mode="json", exclude_none=False)


def owner_state_for_prompt(st: dict[str, Any]) -> dict[str, Any]:
    """Subset of owner state safe to inject into the task model context."""
    normalized = ensure_state_defaults(st)
    return {key: normalized[key] for key in OWNER_STATE_PROMPT_KEYS if key in normalized}


def default_state_dict() -> dict[str, Any]:
    """Create fresh owner state with defaults applied."""
    return ensure_state_defaults({})


def load_owner_state_dict(drive_root: pathlib.Path | Any) -> dict[str, Any]:
    """Return owner state dict with defaults applied."""
    root = pathlib.Path(drive_root)
    store = get_runtime_store(root) or init_runtime_store(root)
    raw = store.load_owner_runtime_state()
    if raw is None:
        return default_state_dict()
    normalized = ensure_state_defaults(dict(raw))
    if set(raw) - set(normalized):
        store.save_owner_runtime_state(normalized)
    return normalized


__all__ = [
    "OWNER_STATE_PROMPT_KEYS",
    "default_state_dict",
    "ensure_state_defaults",
    "load_owner_state_dict",
    "owner_state_for_prompt",
]
