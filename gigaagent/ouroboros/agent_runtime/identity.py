"""The identity a runtime stamps on every record it claims."""

from __future__ import annotations

import uuid

__all__ = ["mint_runtime_id"]


def mint_runtime_id() -> str:
    """A fresh identity for this runtime instance, unique among live instances.

    Random rather than derived: a pid recycles, a hostname is shared by two replicas on
    the same node, and an instance that presented its predecessor's identity would have
    its predecessor's abandoned runs counted as its own.

    Minted by the runtime because the runtime is what the identity names. A worker id names
    a lane inside an instance and is not an address; this is what lets a release or a
    reclaim reason about where a run actually is.
    """
    return uuid.uuid4().hex
