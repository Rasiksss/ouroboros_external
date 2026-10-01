"""The local A2A test servers and the env file that configures an agent to call them.

Nothing in the product imports these servers, so nothing else would notice them rotting: a
renamed argument, a port that drifts away from the env file, an auth wrapper that stops
refusing. The scenarios are driven through the servers' own ASGI apps rather than over
loopback — the same agents a manual run gets, without the ports.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

# Requires a2a-sdk[http-server]==1.0.3 (see pyproject.toml).
pytest.importorskip("a2a.server.routes.jsonrpc_routes", reason="a2a-sdk not installed; skip A2A tests")

from ouroboros.egress.a2a.connection import A2aRemoteAgentConnection
from ouroboros.storage.settings.schema import A2aRemoteAgentEntry
from tests.test_helper.a2a import (
    cardless_agent_server as cardless,
    install_fake_transport,
    open_agent_server as opened,
    secure_agent_server as secure,
)

ENV_PATH = Path(__file__).resolve().parent / "test_helper" / "a2a" / "a2a_agents.env"
DEAD_PORT = 18909


def _env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        values[key.strip()] = raw.strip().strip("'\"")
    return values


def _entries() -> dict[str, A2aRemoteAgentEntry]:
    payload = json.loads(_env()["A2A_REMOTE_AGENTS"])
    return {str(item["id"]): A2aRemoteAgentEntry(**item) for item in payload}


def _run(entry_id: str, body, monkeypatch):
    """Connect the configured entry against the real server apps and run ``body`` on it."""
    install_fake_transport(monkeypatch, opened.AGENT, secure.AGENT, cardless.AGENT)
    entry = _entries()[entry_id].model_copy(update={"poll_interval_sec": 0.05})

    async def scenario():
        connection = A2aRemoteAgentConnection(entry)
        await connection.connect()
        try:
            return await body(connection)
        finally:
            await connection.aclose()

    return asyncio.run(scenario())


# ---------------------------------------------------------------------------
# The env file and the servers have to agree
# ---------------------------------------------------------------------------


def test_the_env_file_addresses_the_servers_it_documents():
    env = _env()
    assert env["A2A_EGRESS_ENABLED"] == "true"
    assert int(env["A2A_TOOL_TIMEOUT_SEC"]) > 0

    entries = _entries()
    assert set(entries) == {
        "local-open",
        "local-secure",
        "local-secure-badtoken",
        "local-cardless",
        "local-dead",
    }
    for module, entry_id in ((opened, "local-open"), (secure, "local-secure"), (cardless, "local-cardless")):
        assert entries[entry_id].url == f"http://{module.HOST}:{module.PORT}"
        assert entry_id == module.AGENT_ID
    # The wrong-token entry has to reach the same agent, or it proves nothing about the token.
    assert entries["local-secure-badtoken"].url == entries["local-secure"].url
    assert entries["local-secure"].auth_token == f"Bearer {secure.AUTH_TOKEN}"
    assert entries["local-secure-badtoken"].auth_token != entries["local-secure"].auth_token
    # The unreachable entry stays unreachable: no server may claim its port.
    assert entries["local-dead"].url.endswith(f":{DEAD_PORT}")
    assert DEAD_PORT not in {opened.PORT, secure.PORT, cardless.PORT}


def test_the_servers_do_not_collide():
    ports = [opened.PORT, secure.PORT, cardless.PORT]
    assert len(set(ports)) == len(ports)
    assert secure.AGENT.card.security_schemes or secure.AGENT.card.security_requirements
    # The open agent publishes no scheme, so a caller knows it needs no credential.
    assert not opened.AGENT.card.security_schemes and not opened.AGENT.card.security_requirements


# ---------------------------------------------------------------------------
# One scenario per configured entry
# ---------------------------------------------------------------------------


def test_the_open_agent_answers_without_a_credential(monkeypatch):
    async def body(connection):
        assert connection.card_available is True
        assert "slow — " in connection.schema.description
        return await connection.call("hello there", timeout_sec=10.0)

    assert "answer: hello there" in _run("local-open", body, monkeypatch)


def test_the_secure_agent_answers_the_configured_token(monkeypatch):
    async def body(connection):
        assert connection.card_available is True
        return await connection.call("hello guarded", timeout_sec=10.0)

    assert "answer: hello guarded" in _run("local-secure", body, monkeypatch)


def test_the_wrong_token_reads_the_card_and_is_refused_the_call(monkeypatch):
    async def body(connection):
        # Discovery is deliberately open: that is how a caller learns which scheme to satisfy.
        assert connection.card_available is True
        return await connection.call("hello guarded", timeout_sec=10.0)

    result = _run("local-secure-badtoken", body, monkeypatch)
    assert result.startswith("⚠️ A2A_ERROR (local-secure-badtoken)")
    assert "401" in result or "Unauthorized" in result or "unauthorized" in result


def test_the_cardless_agent_degrades_to_a_bare_tool_that_still_works(monkeypatch):
    async def body(connection):
        assert connection.card_available is False
        assert set(connection.schema.parameters["properties"]) == {"message", "wait", "task_id"}
        return await connection.call("hello anyway", timeout_sec=10.0)

    assert "answer: hello anyway" in _run("local-cardless", body, monkeypatch)


def test_the_dead_agent_is_offered_but_every_call_fails(monkeypatch):
    async def body(connection):
        assert connection.card_available is False
        return await connection.call("anyone home", timeout_sec=10.0)

    assert _run("local-dead", body, monkeypatch).startswith("⚠️ A2A_ERROR (local-dead)")
