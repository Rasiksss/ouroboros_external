from __future__ import annotations

import os
import sys
from pathlib import Path

# Runnable as a script, so the repo root has to be importable before the helper is.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import uvicorn

from tests.test_helper.a2a.scripted_agent import DEFAULT_SKILLS, build_scripted_agent

SCHEME = "https"
HOST = "127.0.0.1"
PORT = 18902
AGENT_ID = "local-secure"
AUTH_HEADER = "Authorization"
AUTH_TOKEN = "local-a2a-secret"
TLS_CERTFILE_ENV = "A2A_TEST_TLS_CERTFILE"
TLS_KEYFILE_ENV = "A2A_TEST_TLS_KEYFILE"

AGENT = build_scripted_agent(
    AGENT_ID,
    base_url=f"{SCHEME}://{HOST}:{PORT}",
    name="Local Secure Agent",
    description="Scripted A2A agent for local testing; JSON-RPC needs a bearer token.",
    skills=DEFAULT_SKILLS,
    accepted_tokens=frozenset({AUTH_TOKEN}),
)

if __name__ == "__main__":
    uvicorn.run(
        AGENT.app,
        host=HOST,
        port=PORT,
        log_level="info",
        ssl_certfile=os.environ[TLS_CERTFILE_ENV],
        ssl_keyfile=os.environ[TLS_KEYFILE_ENV],
    )
