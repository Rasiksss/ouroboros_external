from __future__ import annotations

from contextlib import contextmanager

import pytest

from tests.helpers.server_client import patched_server_client
from transport.browser.contracts import (
    BrowserHealthResponse,
    StateResponse,
)

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
            "transport.browser.state._build_state_payload",
            side_effect=RuntimeError("state unavailable"),
        ):
            response = client.get("/api/state")
    assert response.status_code == 500
    payload = response.json()
    assert payload == {"error": "failed to load runtime state", "ok": False}
    assert "Traceback" not in str(payload)


@contextmanager
def _trusted_identity():
    from tests.helpers.agent_registry import FakeAgentRegistry
    from transport.browser.identity import TrustedHeaderIdentity, browser_identity

    previous = browser_identity.resolver
    browser_identity.initialize(TrustedHeaderIdentity(FakeAgentRegistry()))
    try:
        yield
    finally:
        browser_identity.initialize(previous)


def _headers(owner: str, chat: int = 7) -> dict[str, str]:
    from transport.browser.identity import CHAT_HEADER_NAME, OWNER_HEADER_NAME

    return {OWNER_HEADER_NAME: owner, CHAT_HEADER_NAME: str(chat)}


def test_state_reports_the_askers_queue_and_refuses_an_unidentified_request(monkeypatch, tmp_path):
    """The route answers about the caller, and fail-closed when it cannot tell who that is."""
    from new_core.identity import OwnerScope

    seen: list[str] = []

    def _runtime_status(owner_scope: OwnerScope) -> dict[str, int]:
        seen.append(owner_scope.owner_id)
        return {
            "workers_total": 2,
            "workers_busy": 0,
            "pending_count": 1 if owner_scope.owner_id == "alice" else 0,
            "running_count": 0,
            "spent_calls": 0,
        }

    with _browser_ingress_client(monkeypatch, tmp_path) as client, _trusted_identity():
        client.app.state.runtime_status = _runtime_status
        alice = client.get("/api/state", headers=_headers("alice"))
        bob = client.get("/api/state", headers=_headers("bob"))
        unidentified = client.get("/api/state")

    assert alice.json()["pending_count"] == 1
    assert bob.json()["pending_count"] == 0
    assert seen == ["alice", "bob"]
    assert unidentified.status_code == 403
    # Lanes belong to this process, not to the asker.
    assert alice.json()["workers_total"] == 2
