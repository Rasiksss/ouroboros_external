"""Local A2A test agent — bearer token required, agent card still public.

Run standalone:

    .venv/bin/python tests/test_helper/a2a/secure_agent_server.py

Listens on http://127.0.0.1:18902
Requires header: Authorization: Bearer local-a2a-secret
The card stays readable without it, which is what lets a caller discover the scheme; JSON-RPC
without a matching token is 401. ``a2a_agents.env`` configures both a correct credential
(``local-secure``) and a wrong one (``local-secure-badtoken``).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Runnable as a script, so the repo root has to be importable before the helper is.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import uvicorn

from tests.test_helper.a2a.scripted_agent import DEFAULT_SKILLS, build_scripted_agent

HOST = "127.0.0.1"
PORT = 18902
AGENT_ID = "local-secure"
AUTH_HEADER = "Authorization"
AUTH_TOKEN = "local-a2a-secret"

AGENT = build_scripted_agent(
    AGENT_ID,
    base_url=f"http://{HOST}:{PORT}",
    name="Local Secure Agent",
    description="Scripted A2A agent for local testing; JSON-RPC needs a bearer token.",
    skills=DEFAULT_SKILLS,
    require_token=AUTH_TOKEN,
)

if __name__ == "__main__":
    uvicorn.run(AGENT.app, host=HOST, port=PORT, log_level="info")
