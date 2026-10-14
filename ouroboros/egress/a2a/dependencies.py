from __future__ import annotations

from dataclasses import dataclass

from ouroboros.egress.a2a.task_handles import A2aTaskHandles
from ouroboros.egress.auth.interfaces import EgressAuthDependencies


@dataclass(frozen=True)
class A2aEgressDependencies:
    auth: EgressAuthDependencies
    task_handles: A2aTaskHandles


__all__ = ["A2aEgressDependencies"]
