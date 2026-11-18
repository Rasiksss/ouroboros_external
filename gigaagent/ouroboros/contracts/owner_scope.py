"""Instance identity constants, and the identity of work no user asked for.

The ``OwnerScope`` type lives in ``new_core.identity`` so the loop layer can name it. What
lives here is what the engine cannot read for itself: the instance's configured owner and
agent. A transport builds its own scope at ingress from these two plus its own
``TRANSPORT_NAME`` — none of the four axes is ever read from a message.
"""

from __future__ import annotations

from new_core.identity import OwnerScope, ServiceJob, ServiceScope, SurfaceId

#: The instance's single owner, where nothing resolved a real one. The identity of the
#: instance is this module's, not the settings table's module's.
SETTINGS_OWNER_ID = "global"

SINGLE_AGENT_ID = "main"

OWNER_HEADER_NAME = "X-Ouroboros-Owner-Id"
CHAT_HEADER_NAME = "X-Ouroboros-Chat-Id"
AGENT_HEADER_NAME = "X-Ouroboros-Agent-Id"


def configured_owner_id() -> str:
    """The instance's single owner. One owner per instance at this stage.

    Read by a transport when it builds the scope for an arriving message, and by nothing
    that holds a message: an owner a sender could place in its own payload and have
    believed is the one thing this value must never be.
    """
    return SETTINGS_OWNER_ID


def configured_agent_id() -> str:
    """The instance's single agent. One agent per instance at this stage."""
    return SINGLE_AGENT_ID


def service_identity(*, job: ServiceJob) -> ServiceScope:
    """The identity of work no user asked for.

    Such work still owns rows and still has to say whose they are, so it gets the same
    owner and agent as everything else on this instance. It names its job rather than a
    chat, because there is no conversation behind it — and it is not an ``OwnerScope``,
    so it cannot reach anything that requires an author. Where a storage call does require
    one, the widening is spelled out: ``service_identity(job=…).as_scope()``.
    """
    return ServiceScope(owner_id=configured_owner_id(), agent_id=configured_agent_id(), job=job)


def stored_pair_identity(*, owner_id: str, agent_id: str, job: ServiceJob) -> ServiceScope:
    """The identity of author-less work over one *stored* owner/agent pair.

    A deployment-wide sweep still has to address one owner at a time once storage is keyed
    by owner. The pair comes from the rows being swept, never from configuration and never
    from a message, and the job says why the work exists.
    """
    return ServiceScope(owner_id=owner_id, agent_id=agent_id, job=job)


__all__ = [
    "AGENT_HEADER_NAME",
    "CHAT_HEADER_NAME",
    "OWNER_HEADER_NAME",
    "SETTINGS_OWNER_ID",
    "SINGLE_AGENT_ID",
    "OwnerScope",
    "ServiceJob",
    "ServiceScope",
    "SurfaceId",
    "configured_agent_id",
    "configured_owner_id",
    "service_identity",
    "stored_pair_identity",
]
