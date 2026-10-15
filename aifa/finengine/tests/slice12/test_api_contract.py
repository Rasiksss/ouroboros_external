from __future__ import annotations

from starlette.requests import Request

from finengine.api.main import _event_cursor, _typed_run_response, app


def test_versioned_result_event_routes_coexist_with_legacy_streams():
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/extract/runs/{run_id}/events" in paths
    assert "/chat/runs/{run_id}/events" in paths
    assert "/extract/runs/{run_id}/stream" in paths
    assert "/chat/runs/{run_id}/stream" in paths


def test_sse_contract_exposes_version_and_standard_replay_cursor():
    response = _typed_run_response("run-1", kind="chat", after_sequence=7)
    assert response.headers["x-result-event-version"] == "1.0"
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/chat/runs/run-1/events",
        "headers": [(b"last-event-id", b"7")],
        "query_string": b"after=3",
    })
    assert _event_cursor(request) == 7
