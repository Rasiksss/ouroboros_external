from __future__ import annotations

import pytest

from ouroboros.gateway.contracts import (
    BrowserHealthResponse,
    StateResponse,
)
from tests.helpers.server_client import patched_server_client

HEALTH_KEYS = set(BrowserHealthResponse.__annotations__.keys())
STATE_KEYS = set(StateResponse.__annotations__.keys())


def _browser_ingress_client(monkeypatch, tmp_path):
    return patched_server_client(monkeypatch, tmp_path, browser_enabled=True)


def test_health_response_shape(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert HEALTH_KEYS.issubset(payload.keys())
    assert "version" in payload
    assert "runtime_version" in payload
    assert "app_version" in payload


def test_state_response_shape(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as client:
        response = client.get("/api/state")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == STATE_KEYS
    assert "spent_usd" not in payload
    assert "budget_limit" not in payload
    assert "budget_pct" not in payload
    assert "evolution_state" not in payload
    assert "bg_consciousness_state" not in payload
    assert isinstance(payload["uptime"], int)
    assert payload["uptime"] >= 0


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/api/health?debug=1", id="health"),
        pytest.param("/api/state?limit=10", id="state"),
    ],
)
def test_browser_endpoints_reject_unknown_query_parameters(monkeypatch, tmp_path, path):
    with _browser_ingress_client(monkeypatch, tmp_path) as client:
        response = client.get(path)
    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert "error" in payload
    assert "Traceback" not in payload["error"]


def test_state_error_response_has_no_stack_trace(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as client:
        from unittest.mock import patch

        with patch(
            "ouroboros.gateway.browser_state._build_state_payload",
            side_effect=RuntimeError("state unavailable"),
        ):
            response = client.get("/api/state")
    assert response.status_code == 500
    payload = response.json()
    assert payload == {"error": "failed to load runtime state", "ok": False}
    assert "Traceback" not in str(payload)
