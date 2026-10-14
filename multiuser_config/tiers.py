from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class Tier:
    module: str
    paths: tuple[str, ...]


TIERS: Mapping[str, Tier] = MappingProxyType(
    {
        "transport": Tier("multiuser_config.transport.browser", ("/ws", "/api/chat")),
        "supervisor": Tier("multiuser_config.supervisor", ()),
        "agent-runtime": Tier("multiuser_config.agent_runtime.run", ()),
        "background-memory": Tier("multiuser_config.background_memory.run", ()),
        "config-registry": Tier(
            "multiuser_config.config_registry",
            ("/api/agents", "/api/owner-settings", "/api/chats"),
        ),
    }
)
