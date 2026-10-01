"""Remote A2A agent list for this image — the security-owned source of the list.

Mirrors `mcp.py`: editing this file is the only way to change which agents this one delegates to,
so a change goes through code review, CI and image signing. The list is built once at import time
and is fixed for the process lifetime; secrets are read here, never written here.
"""

import os

from ouroboros.storage.settings.schema import A2aRemoteAgentEntry
from ouroboros.storage.settings.store import load_settings


def _token(name: str) -> str:
    """Secret for `name` from the environment — replace the body with the team's secret engine."""
    return os.getenv(name, "")


_polygon = load_settings().OUROBOROS_POLIGON

a2a_remote_agents: list[A2aRemoteAgentEntry]

if _polygon == "DEV":
    # Legacy path: the list still comes from settings, as before this file existed.
    a2a_remote_agents = list(load_settings().A2A_REMOTE_AGENTS)
elif _polygon == "EXAMPLE":
    a2a_remote_agents = [
        A2aRemoteAgentEntry(
            id="example-agent",
            name="Example Agent",
            url="https://agent.example.internal",
            auth_header="Authorization",
            auth_token=_token("A2A_EXAMPLE_TOKEN"),
            featured_skills=["research"],
            retrieval_aliases=["example"],
            poll_interval_sec=2.0,
        ),
    ]
else:
    a2a_remote_agents = []
