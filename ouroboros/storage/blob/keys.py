"""Object key construction for StorageProvider backends."""

from __future__ import annotations

import re

from ouroboros.storage.blob.scopes import ALL_SCOPES

_SCOPE_PART_RE = re.compile(r"^[a-z0-9_-]+$")


def storage_tenant_id() -> str:
    from ouroboros.storage.settings.store import load_settings

    raw = str(load_settings().OUROBOROS_STORAGE_TENANT_ID or "").strip()
    return raw or "default"


def storage_key_prefix() -> str:
    from ouroboros.storage.settings.store import load_settings

    extra = str(load_settings().OUROBOROS_S3_PREFIX or "").strip().strip("/")
    tenant = storage_tenant_id()
    base = f"tenants/{tenant}"
    return f"{base}/{extra}" if extra else base


def build_scope_prefix(scope: str) -> str:
    """Return S3 list prefix for a scope: tenants/{tenant}/{scope}/."""
    scope_norm = str(scope or "").strip().lower()
    if scope_norm not in ALL_SCOPES:
        raise ValueError(f"Unknown storage scope: {scope!r}")
    return f"{storage_key_prefix()}/{scope_norm}/"


def build_object_key(scope: str, key: str) -> str:
    """Return full object key: tenants/{tenant}/{scope}/{key}."""
    rel = normalize_storage_key(key)
    return f"{build_scope_prefix(scope)}{rel}"


def normalize_storage_key(key: str) -> str:
    text = str(key or "").replace("\\", "/").strip().lstrip("/")
    if not text or ".." in text.split("/"):
        raise ValueError("Invalid storage key")
    return text


def parse_data_rel_path(rel: str) -> tuple[str, str]:
    """Map a DATA_DIR-relative path to (scope, key).

    Examples:
        memory/identity.md -> (memory, identity.md)
        state/skills/foo/enabled.json -> (state, skills/foo/enabled.json)
    """
    norm = normalize_storage_key(rel)
    parts = norm.split("/", 1)
    scope = parts[0].lower()
    if scope not in ALL_SCOPES:
        raise ValueError(f"Path {rel!r} is not under a known storage scope")
    storage_key = parts[1] if len(parts) > 1 else ""
    if not storage_key:
        raise ValueError(f"Path {rel!r} has no key within scope {scope}")
    return scope, storage_key
