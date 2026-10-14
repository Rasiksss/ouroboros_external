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
from ouroboros.storage.settings.schema import A2aRemoteAgentEntry
from tests.test_helper.a2a.scripted_agent import ScriptedAgent, build_scripted_agent

FAKE_HOST = "https://fake-a2a.test"


def build_fake_agent(agent_id: str = "fake", **options) -> ScriptedAgent:
    return build_scripted_agent(agent_id, base_url=FAKE_HOST, mount=f"/{agent_id}", **options)


class _UnreachableTransport(httpx.AsyncBaseTransport):
    """Every request fails to connect — a configured agent whose host is simply not there."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)


def install_fake_transport(monkeypatch, *agents: ScriptedAgent) -> None:
    by_url = {agent.url.rstrip("/"): agent for agent in agents}
    real_build = transports.build_http_client

    def _build(entry: A2aRemoteAgentEntry, forwarded_headers: frozenset[str]) -> httpx.AsyncClient:
        client = real_build(entry, forwarded_headers)
        agent = by_url.get(entry.url.rstrip("/"))
        client._transport = httpx.ASGITransport(app=agent.app) if agent is not None else _UnreachableTransport()
        return client

    monkeypatch.setattr(transports, "build_http_client", _build)


__all__ = [
    "FAKE_HOST",
    "ScriptedAgent",
    "build_fake_agent",
    "install_fake_transport",
]
