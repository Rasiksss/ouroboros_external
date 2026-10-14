from __future__ import annotations

from tests.helpers.server_client import patched_server_client


def _patched_server_client(monkeypatch, tmp_path, *, browser_enabled: bool):
    return patched_server_client(monkeypatch, tmp_path, browser_enabled=browser_enabled)


def test_browser_ingress_routes_absent_when_disabled(monkeypatch, tmp_path):
    with _patched_server_client(monkeypatch, tmp_path, browser_enabled=False) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/chat/history").status_code == 404
        assert client.post("/api/chat/upload").status_code == 404
        assert client.delete("/api/chat/upload").status_code == 404
        assert client.get("/api/settings").status_code == 404


def test_browser_ingress_ws_absent_when_disabled(monkeypatch, tmp_path):
    with _patched_server_client(monkeypatch, tmp_path, browser_enabled=False) as client:
        try:
            with client.websocket_connect("/ws"):
                raised = False
        except Exception:
            raised = True
        assert raised
