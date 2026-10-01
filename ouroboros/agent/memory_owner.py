"""Owner namespace resolution for managed memory."""

from __future__ import annotations

from typing import Any


def resolve_memory_owner_id(task: dict[str, Any] | None = None) -> str:
    """Return the single managed-memory namespace.

    Task/user metadata is intentionally ignored: the product is single-user and
    splitting managed memory by transient task payload fields makes memories
    appear to disappear.
    """
    del task
    return "global"
