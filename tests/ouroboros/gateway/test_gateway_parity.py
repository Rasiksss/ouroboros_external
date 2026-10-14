from __future__ import annotations

from ouroboros.gateway.router import collect_routes
from transport.browser.contracts import HTTP_ENDPOINTS


def test_gateway_package_preserves_route_collector_export():
    from ouroboros.gateway import collect_routes as package_collect_routes

    assert package_collect_routes is collect_routes


def test_gateway_contract_endpoint_index_matches_router(tmp_path):
    tokens: set[str] = set()
    for route in collect_routes(data_dir=tmp_path):
        path = getattr(route, "path", "")
        if not path:
            continue
        methods = getattr(route, "methods", None)
        if methods is None:
            tokens.add(f"WS {path}")
            continue
        normalized = sorted(m for m in methods if m not in {"HEAD", "OPTIONS"})
        if set(normalized) == {"DELETE", "GET", "PATCH", "POST", "PUT"}:
            tokens.add(f"ANY {path}")
        else:
            for method in normalized:
                tokens.add(f"{method} {path}")
    contract_tokens = set(HTTP_ENDPOINTS)
    missing = contract_tokens - tokens
    extra = tokens - contract_tokens
    assert not missing, f"HTTP_ENDPOINTS includes routes not mounted by gateway.router: {sorted(missing)}"
    assert not extra, f"gateway.router mounts routes missing from HTTP_ENDPOINTS: {sorted(extra)}"
