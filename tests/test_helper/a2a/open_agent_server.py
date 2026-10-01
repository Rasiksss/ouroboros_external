"""Local A2A test agent — open endpoint, agent card published.

Run standalone:

    .venv/bin/python tests/test_helper/a2a/open_agent_server.py

Listens on http://127.0.0.1:18901 — card at /.well-known/agent-card.json, JSON-RPC at /.
What to send it is the scenario table in ``scripted_agent.py``; ``a2a_agents.env`` wires it up
as the ``local-open`` remote agent.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Runnable as a script, so the repo root has to be importable before the helper is.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import uvicorn

from tests.test_helper.a2a.scripted_agent import DEFAULT_SKILLS, build_scripted_agent

HOST = "127.0.0.1"
PORT = 18901
AGENT_ID = "local-open"

AGENT = build_scripted_agent(
    AGENT_ID,
    base_url=f"http://{HOST}:{PORT}",
    name="Local Open Agent",
    description="Scripted A2A agent for local testing; needs no credential.",
    skills=DEFAULT_SKILLS,
)

if __name__ == "__main__":
    uvicorn.run(AGENT.app, host=HOST, port=PORT, log_level="info")
