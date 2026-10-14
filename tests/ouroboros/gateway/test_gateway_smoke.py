from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from ouroboros.gateway.router import collect_routes


def test_gateway_core_routes_and_health_shape(tmp_path):
    routes = collect_routes(data_dir=tmp_path)
    paths = {getattr(route, "path", "") for route in routes}
    assert "/api/health" in paths
    assert "/api/state" not in paths
    assert "/start" in paths
    assert "/ready" in paths
    assert "/live" in paths
    assert "/api/settings" not in paths
    assert "/ws" not in paths
    app = Starlette(routes=routes)
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload == {"status": "ok"}
