"""What a2a takes from the composition root, as ABCs it can be handed.

Everything an A2A server needs beyond the seam — the skills its card advertises,
the readiness of the supervisor it submits to — lives above the transport layer.
Naming them here is what keeps ``transport/`` free of ``new_core`` and
``ouroboros.app``: the implementations are wired in the composition root and
passed in, so an instance without one cannot be constructed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSkillSpec:
    """One skill on the agent card, in plain types: the proto stays inside a2a."""

    id: str
    name: str
    description: str
    tags: tuple[str, ...] = ()


class IAgentSkillCatalog(ABC):
    """The skills the A2A agent card advertises."""

    @abstractmethod
    def skills(self) -> list[AgentSkillSpec]:
        """The published set, read on every agent-card request."""


class ISupervisorReadiness(ABC):
    """Waits until the supervisor can accept a submission."""

    @abstractmethod
    async def wait_ready(self, *, timeout_sec: float) -> None:
        """Return once submissions are accepted; raise when they will not be."""


__all__ = [
    "AgentSkillSpec",
    "IAgentSkillCatalog",
    "ISupervisorReadiness",
]
