"""Run existing GigaAgent/AIFA clients against the imported Workstation API."""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "gigaagent"))
sys.path.insert(0, str(ROOT / "aifa/finengine/src"))

from finengine.skills.spec import sealed_room  # noqa: E402 - isolated service import roots above
from ouroboros.egress.workstation.client import WorkstationClient, WorkstationConfig  # noqa: E402

from app.core.config import settings  # noqa: E402


class AsgiSyncTransport(httpx.BaseTransport):
    def __init__(self, app, loop):
        self.app, self.loop = app, loop

    async def send(self, request):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://station") as client:
            response = await client.send(request)
            return httpx.Response(
                response.status_code, headers=response.headers, content=await response.aread(), request=request
            )

    def handle_request(self, request):
        return asyncio.run_coroutine_threadsafe(self.send(request), self.loop).result(timeout=15)


@pytest.mark.asyncio
async def test_both_clients_create_authenticated_sessions(app, monkeypatch):
    monkeypatch.setattr(settings, "IS_CREATE_SESSION_SECRET", True)
    monkeypatch.setattr(settings, "SESSION_CREATE_SECRET", "common-secret")
    monkeypatch.setattr(sealed_room.settings, "workstation_create_secret", "common-secret")
    transport = AsgiSyncTransport(app, asyncio.get_running_loop())
    config = WorkstationConfig(base_url="http://station", create_secret="common-secret", max_attempts=1)
    agent = WorkstationClient(config, client_factory=lambda **kwargs: httpx.Client(transport=transport, **kwargs))
    # Existing agent client sends protocol, session id and shared create secret,
    # then uses the returned bearer token for subsequent operations.
    result = await asyncio.to_thread(agent.create_session, "gigaagent-contour")
    assert result.token and result.created
    state = await asyncio.to_thread(agent.get_session, result.session_id, result.token)
    assert state.session_id == result.session_id

    def create_aifa_room():
        with httpx.Client(base_url="http://station", transport=transport) as client:
            return sealed_room._open_room(client, sealed_room.RoomSession("aifa-contour"))

    assert await asyncio.to_thread(create_aifa_room)
    agent.close()
