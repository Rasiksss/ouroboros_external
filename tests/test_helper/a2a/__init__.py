"""Fake remote A2A agents for the test suite, and runnable ones for local testing.

Two ways to reach the same scripted agent:

* **In-process** — :func:`build_fake_agent` plus :func:`install_fake_transport` point the
  outbound client's HTTP clients at an ASGI app, so a test crosses the whole client without a
  port. Everything above ``build_http_client`` stays the real code path.
* **Over a port** — ``open_agent_server.py``, ``secure_agent_server.py`` and
  ``cardless_agent_server.py`` next to this file serve the same agent for a real process to
  call; ``a2a_agents.env`` configures an agent to call them.

The body itself, and the scenarios it can be asked for, live in :mod:`scripted_agent`.
"""

from __future__ import annotations

import httpx

from ouroboros.egress.a2a import transports
from tests.test_helper.a2a.scripted_agent import ScriptedAgent, build_scripted_agent

#: A host that resolves nowhere: in-process agents are reached by transport, not by DNS.
FAKE_HOST = "http://fake-a2a.test"


def build_fake_agent(agent_id: str = "fake", **options) -> ScriptedAgent:
    """A fake agent ready to be mounted at ``{FAKE_HOST}/{agent_id}``.

    ``options`` are :func:`build_scripted_agent`'s — ``name``, ``description``, ``skills``,
    ``require_token``, ``serve_card``.
    """
    return build_scripted_agent(agent_id, base_url=FAKE_HOST, mount=f"/{agent_id}", **options)


class _UnreachableTransport(httpx.AsyncBaseTransport):
    """Every request fails to connect — a configured agent whose host is simply not there."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)


def install_fake_transport(monkeypatch, *agents: ScriptedAgent) -> None:
    """Point the outbound client's HTTP clients at these in-process apps.

    ``build_http_client`` is the one place the connection decides how it reaches the network,
    so everything above it stays the real code path. An agent not listed here gets a transport
    that refuses to connect — how "unreachable" is spelled.
    """
    by_url = {agent.url.rstrip("/"): agent for agent in agents}

    def _build(entry):
        agent = by_url.get(str(entry.url or "").rstrip("/"))
        transport: httpx.AsyncBaseTransport = (
            httpx.ASGITransport(app=agent.app) if agent is not None else _UnreachableTransport()
        )
        return httpx.AsyncClient(
            transport=transport,
            headers=transports.auth_headers(entry),
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    monkeypatch.setattr(transports, "build_http_client", _build)


__all__ = [
    "FAKE_HOST",
    "ScriptedAgent",
    "build_fake_agent",
    "install_fake_transport",
]
