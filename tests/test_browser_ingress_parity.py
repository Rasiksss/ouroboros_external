from __future__ import annotations

from starlette.routing import WebSocketRoute

from ouroboros.gateway.browser_router import collect_routes
from ouroboros.gateway.contracts import (
    BROWSER_HTTP_ENDPOINTS,
    EXCLUDED_BROWSER_HTTP_ENDPOINTS,
)
from tests.helpers.server_client import patched_server_client


def _route_tokens(routes) -> set[str]:
    tokens: set[str] = set()
    for route in routes:
        path = getattr(route, "path", "")
        if not path:
            continue
        if isinstance(route, WebSocketRoute):
            tokens.add(f"WS {path}")
            continue
        methods = getattr(route, "methods", None)
        if methods is None:
            continue
        normalized = sorted(method for method in methods if method not in {"HEAD", "OPTIONS"})
        for method in normalized:
            tokens.add(f"{method} {path}")
    return tokens


def test_browser_ingress_contract_endpoint_index_matches_router(tmp_path):
    tokens = _route_tokens(collect_routes(data_dir=tmp_path, repo_dir=tmp_path))
    contract_tokens = set(BROWSER_HTTP_ENDPOINTS)
    missing = contract_tokens - tokens
    extra = tokens - contract_tokens
    assert not missing, f"HTTP_ENDPOINTS includes routes not mounted by browser router: {sorted(missing)}"
    assert not extra, f"browser router mounts routes missing from HTTP_ENDPOINTS: {sorted(extra)}"


def test_browser_ingress_contract_excludes_settings_and_http_control():
    indexed = set(BROWSER_HTTP_ENDPOINTS)
    assert not indexed & EXCLUDED_BROWSER_HTTP_ENDPOINTS, (
        "BROWSER_HTTP_ENDPOINTS must not include out-of-scope routes: "
        f"{sorted(indexed & EXCLUDED_BROWSER_HTTP_ENDPOINTS)}"
    )


def test_browser_ingress_http_control_routes_not_mounted_when_enabled(monkeypatch, tmp_path):
    with patched_server_client(monkeypatch, tmp_path, browser_enabled=True) as client:
        assert client.post("/api/command", json={"cmd": "/help"}).status_code == 404
        assert client.post("/api/reset").status_code == 404
