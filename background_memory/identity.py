"""The identity a background-memory instance presents on the leases it holds."""

from __future__ import annotations

import uuid

__all__ = ["mint_holder_id"]


def mint_holder_id() -> str:
    """A fresh identity for this instance, unique among the live ones.

    Random rather than derived: a pid recycles, a hostname is shared by two replicas on the
    same node, and an instance presenting its predecessor's identity would renew and release
    leases the predecessor took.

    Minted here rather than imported from ``ouroboros/agent_runtime/identity.py``, which is
    the same idea with the same reasoning: that package is the agent tier, and this one
    reaches into neither the agent nor the supervisor.
    """
    return uuid.uuid4().hex
