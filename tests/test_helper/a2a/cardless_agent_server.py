"""Local A2A test agent — serves JSON-RPC but publishes no agent card.

Run standalone:

    .venv/bin/python tests/test_helper/a2a/cardless_agent_server.py

Listens on http://127.0.0.1:18903 — JSON-RPC at /, and 404 for
/.well-known/agent-card.json. That is the degraded case worth seeing by hand: the caller
cannot read the skills, so it offers a bare ``{message}`` tool, and calls still go through.
``a2a_agents.env`` wires it up as the ``local-cardless`` remote agent.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Runnable as a script, so the repo root has to be importable before the helper is.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import uvicorn

from tests.test_helper.a2a.scripted_agent import DEFAULT_SKILLS, build_scripted_agent

HOST = "127.0.0.1"
PORT = 18903
AGENT_ID = "local-cardless"

AGENT = build_scripted_agent(
    AGENT_ID,
    base_url=f"http://{HOST}:{PORT}",
    name="Local Cardless Agent",
    description="Scripted A2A agent for local testing; its agent card is not published.",
    skills=DEFAULT_SKILLS,
    serve_card=False,
)

if __name__ == "__main__":
    uvicorn.run(AGENT.app, host=HOST, port=PORT, log_level="info")
