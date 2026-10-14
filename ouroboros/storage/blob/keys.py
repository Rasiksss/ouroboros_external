"""Object key construction for StorageProvider backends.

Two axes decide a physical key: the storage *scope* (``uploads``, ``state``, …) and, under
the multi-owner mode and for the scopes in ``OWNER_KEYED_SCOPES``, the *owner scope* the
call is made on behalf of. The owner segment sits below the scope so ``tenants/{tenant}/uploads/`` stays a
stable operator-visible prefix and the scope-name walk in ``resolve_drive_root`` keeps
finding the data root.
"""

from __future__ import annotations

import hashlib
import re

from new_core.identity import OwnerScope
from ouroboros.storage.blob.scopes import ALL_SCOPES, OWNER_KEYED_SCOPES, RETIRED_SCOPES

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


def owner_key_segment(value: str) -> str:
    """A path-safe, injective and legible segment for one identity axis.

    An owner id is an opaque string a proxy chose, so it may carry separators, dots or
    300 characters. Sanitizing alone is not injective — two different ids can sanitize
    alike — so the hash suffix decides, and the readable part is what an operator sees
    in a bucket listing.
    """
    raw = str(value or "")
    sanitized = re.sub(r"[^a-z0-9_-]", "_", raw.lower())[:48]
    if not _SCOPE_PART_RE.fullmatch(sanitized):
        raise ValueError(f"Cannot build an owner key segment from {value!r}")
    return f"{sanitized}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


def safe_key_segment(value: str, fallback: str) -> str:
    """One path segment built from an arbitrary identifier: task ids, tool call ids.

    Not ``owner_key_segment``: this one is not injective and is not meant to be. It names
    something the runtime already generated, where a collision is a name clash and not a
    crossing between two people.
    """
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
    return text[:80] or fallback


def normalized_scope(scope: str) -> str:
    scope_norm = str(scope or "").strip().lower()
    if scope_norm not in ALL_SCOPES:
        raise ValueError(f"Unknown storage scope: {scope!r}")
    return scope_norm


def serves_several_owners() -> bool:
    from ouroboros.storage.settings.store import load_settings

    return bool(load_settings().OUROBOROS_MULTIUSER)


def scope_crosses_owners(scope: str) -> bool:
    return serves_several_owners() and normalized_scope(scope) not in OWNER_KEYED_SCOPES


def owner_scope_prefix(owner_scope: OwnerScope, scope: str) -> str:
    if normalized_scope(scope) not in OWNER_KEYED_SCOPES or not serves_several_owners():
        return ""
    owner = owner_key_segment(owner_scope.owner_id)
    agent = owner_key_segment(owner_scope.agent_id)
    return f"owners/{owner}/agents/{agent}/"


def build_scope_prefix(owner_scope: OwnerScope, scope: str) -> str:
    """Return the S3 list prefix for one owner within a scope."""
    scope_norm = normalized_scope(scope)
    return f"{storage_key_prefix()}/{scope_norm}/{owner_scope_prefix(owner_scope, scope_norm)}"


def build_object_key(owner_scope: OwnerScope, scope: str, key: str) -> str:
    """Return the full object key for one owner within a scope."""
    return f"{build_scope_prefix(owner_scope, scope)}{normalize_storage_key(key)}"


def normalize_storage_key(key: str) -> str:
    text = str(key or "").replace("\\", "/").strip().lstrip("/")
    if not text or ".." in text.split("/"):
        raise ValueError("Invalid storage key")
    return text


def retired_scope_for_rel(rel: str) -> str:
    text = str(rel or "").replace("\\", "/").strip().lstrip("/")
    head = text.split("/", 1)[0].lower()
    return head if head in RETIRED_SCOPES else ""


def parse_data_rel_path(rel: str) -> tuple[str, str]:
    """Map a logical DATA_DIR-relative path to a storage scope and key."""
    norm = normalize_storage_key(rel)
    parts = norm.split("/", 1)
    scope = parts[0].lower()
    if scope not in ALL_SCOPES:
        raise ValueError(f"Path {rel!r} is not under a known storage scope")
    storage_key = parts[1] if len(parts) > 1 else ""
    if not storage_key:
        raise ValueError(f"Path {rel!r} has no key within scope {scope}")
    return scope, storage_key
