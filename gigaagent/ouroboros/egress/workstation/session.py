"""Session identity for AEF WorkStation: scope key -> session_id (spec §4)."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field

#: Random per process, never persisted (spec §4.2). After a restart the same
#: scope key hashes to a different `ephemeral` session_id; the old session
#: becomes unreachable and the service closes it itself on idle/TTL.
PROCESS_INSTANCE_ID = uuid.uuid4().hex


def scope_key_for(*parts: object) -> str:
    """Deterministic scope key from an arbitrary number of identifying parts (spec §4.1).

    The primary caller passes the conversation ``chat_id`` so that every task
    in the same chat reuses a single WorkStation session.  Variadic on
    purpose: extra positional parts (e.g. owner id) can be added later
    without changing this function's signature.
    """
    joined = "_".join(text for part in parts if (text := str(part or "").strip()))
    return joined or "main"


def session_id_for(strategy: str, scope_key: str) -> str:
    """Hash a scope key into a session id (spec §4.2)."""
    components = ["workstation", strategy]
    if strategy == "ephemeral":
        components.append(PROCESS_INSTANCE_ID)
    components.append(scope_key)
    digest = hashlib.sha256(":".join(components).encode("utf-8")).hexdigest()
    return digest[:24]


@dataclass
class SessionRecord:
    """In-memory record for a live WorkStation session (spec §6): never persisted."""

    session_id: str
    token: str = field(repr=False)
    created_at: float
    last_used_at: float
    quotas: dict[str, object] = field(default_factory=dict)

    def touch(self) -> None:
        self.last_used_at = time.monotonic()


__all__ = ["PROCESS_INSTANCE_ID", "SessionRecord", "scope_key_for", "session_id_for"]
