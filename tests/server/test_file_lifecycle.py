import asyncio
import threading
from contextlib import asynccontextmanager

import pytest

from tests.helpers.files import FileConsumerRuntime
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import TEST_CREDENTIALS_KEY


@parametrize_from_dict({name: {"failure": name} for name in ("startup", "agent", "browser", "supervisor")})
def test_server_retains_file_clients_until_consumers_stop(monkeypatch, tmp_path, failure):
    import server

    runtime = FileConsumerRuntime(drains=failure != "agent")
    browser = FileConsumerRuntime(drains=False)
    release = threading.Event()
    supervisor = threading.Thread(target=release.wait, daemon=True)
    closed = []
    original = server.open_file_blob

    @asynccontextmanager
    async def file_client(settings):
        async with original(settings) as blob:
            try:
                yield blob
            finally:
                closed.append(runtime.running or browser.running or supervisor.is_alive())

    def start_memory(*args, **kwargs):
        if failure == "startup":
            raise RuntimeError("startup interrupted after agents started")

    monkeypatch.setenv("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY", TEST_CREDENTIALS_KEY)
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", "")
    monkeypatch.setenv("BROWSER_INGRESS_ENABLED", "true" if failure == "browser" else "false")
    monkeypatch.setenv("MCP_ENABLED", "false")
    monkeypatch.setenv("A2A_ENABLED", "false")
    monkeypatch.setenv("A2A_EGRESS_ENABLED", "false")
    monkeypatch.setattr(server, "_agent_runtime_thread", runtime)
    monkeypatch.setattr(server, "get_browser_ws_hub", lambda: browser)
    monkeypatch.setattr(server, "TICK_STOP_BUDGET_SEC", 0.01)
    monkeypatch.setattr(server, "_supervisor_thread", None)
    monkeypatch.setattr(server, "_started_transports", server.StartedTransports())
    monkeypatch.setattr(server, "open_file_blob", file_client)
    monkeypatch.setattr(server, "has_supervisor_provider", lambda _settings: True)
    monkeypatch.setattr(server, "_start_supervisor_if_needed", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_memory_runtime_if_needed", start_memory)
    monkeypatch.setattr(server, "_load_distribution_extensions", lambda *a: {})
    server._stop_requested.clear()

    async def scenario():
        try:
            with pytest.raises(RuntimeError if failure == "startup" else TimeoutError):
                async with server.lifespan(server._starlette_app):
                    assert runtime.running
                    if failure == "supervisor":
                        supervisor.start()
                        server._supervisor_thread = supervisor
            assert runtime.drain_called
            assert closed == ([False] if failure == "startup" else [])
        finally:
            runtime.running = False
            browser.running = False
            release.set()
            if supervisor.ident is not None:
                supervisor.join(1)
            if server._retained_file_resources is not None:
                await server._retained_file_resources.aclose()
                server._retained_file_resources = None
            await server.async_engine_factory.dispose()
            await server.probe_database_factory.dispose()
            server._stop_requested.clear()

    asyncio.run(scenario())
