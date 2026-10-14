"""Scope construction for tests, standing in for what a transport does at ingress.

Production has no shared mint any more: each transport builds its own ``OwnerScope`` from
the instance's configured owner and agent plus its own ``TRANSPORT_NAME``. Tests speak in
delivery sources — ``sberchat``, ``web`` — so they need the lenient read direction as well,
which is why this lives here and not beside the transports.
"""

from __future__ import annotations

from new_core.identity import OwnerScope, SurfaceId
from ouroboros.contracts.owner_scope import configured_agent_id, configured_owner_id


def surface_for(source: str) -> SurfaceId:
    """The closed surface a test's delivery source belongs to."""
    return SurfaceId.coerce(source) or SurfaceId.SKILL_BRIDGE


def scope_for(source: str, chat_id: int | str) -> OwnerScope:
    """The scope a transport would build for a message arriving on ``source``."""
    return OwnerScope(
        owner_id=configured_owner_id(),
        agent_id=configured_agent_id(),
        surface_id=surface_for(source),
        chat_id=str(chat_id),
    )


__all__ = ["scope_for", "surface_for"]
