from __future__ import annotations

import pytest

from tests.helpers.server_client import patched_server_client


def _patched_test_client(monkeypatch, tmp_path):
    return patched_server_client(monkeypatch, tmp_path)


@pytest.mark.parametrize("path", ["/health/readiness", "/health/liveness"])
def test_health_probe_endpoints_return_200(monkeypatch, tmp_path, path: str):
    with _patched_test_client(monkeypatch, tmp_path) as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.text == ""


def test_readiness_returns_503_while_supervisor_starts(monkeypatch, tmp_path) -> None:
    with _patched_test_client(monkeypatch, tmp_path) as client:
        import server as srv

        srv._supervisor_ready.clear()
        response = client.get("/health/readiness")

    assert response.status_code == 503
    assert "supervisor starting" in response.text
