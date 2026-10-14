from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import ouroboros.tools.gigasearch as gs_module
from ouroboros.storage.settings.store import reset_memory_settings_for_tests
from tests.helpers.http_double import FakeHttpResponse, RecordingHttpClient

_NON_JSON = object()

_DEFAULT_GS_PAYLOAD = {
    "documents": [],
    "search_metadata": {"status": "ok", "code": "200", "description": ""},
}


class _GsResponse(FakeHttpResponse):
    """Extends FakeHttpResponse with the gigasearch default payload and _NON_JSON sentinel."""

    def __init__(self, payload: Any = None, *, status_code: int = 200, text: str = "") -> None:
        if payload is _NON_JSON:
            payload = None
            self._raise_on_json = True
        else:
            self._raise_on_json = False
        super().__init__(payload if payload is not None else _DEFAULT_GS_PAYLOAD, status_code=status_code, text=text)

    def json(self) -> Any:
        if self._raise_on_json:
            raise ValueError("response is not JSON")
        return super().json()


@pytest.fixture()
def gs(monkeypatch: pytest.MonkeyPatch):
    """Handler-level fixture: monkeypatches httpx.Client for the full handler flow."""
    instances: list[RecordingHttpClient] = []
    shared_responses: list = []

    def _factory(**kwargs: Any) -> RecordingHttpClient:
        client = RecordingHttpClient(**kwargs)
        client.responses = shared_responses
        instances.append(client)
        return client

    monkeypatch.setenv("GIGASEARCH_BASE_URL", "https://gigasearch.example")
    monkeypatch.setenv("GIGASEARCH_SURFACE_ID", "test-surface")
    monkeypatch.setenv("GIGASEARCH_USER_ID", "test-user")
    monkeypatch.setenv("DLP_BASE_URL", "http://dlp.example:3010")
    monkeypatch.setattr(gs_module.DlpClient, "check", lambda _self, _query: None)
    monkeypatch.setattr(httpx, "Client", _factory)
    reset_memory_settings_for_tests()
    yield SimpleNamespace(
        module=gs_module,
        instances=instances,
        shared_responses=shared_responses,
    )
    reset_memory_settings_for_tests()


def _ctx(tmp_path: pathlib.Path) -> SimpleNamespace:
    return SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path / "data")


def _call(gs: Any, tmp_path: pathlib.Path, **kwargs: Any) -> str:
    return gs.module._gigasearch_handler(_ctx(tmp_path), **kwargs)


def test_schema_shape(gs: Any) -> None:
    schema = gs.module.get_tools()[0].schema

    assert schema.name == "gigasearch"
    assert schema.parameters["required"] == ["query"]
    assert schema.parameters["properties"]["query"]["type"] == "string"
    assert schema.parameters["properties"]["limit"]["type"] == "integer"


def test_description_refuses_internal_identifiers(gs: Any) -> None:
    description = gs.module.get_tools()[0].schema.description

    assert "EPK client ID (19 digits)" in description
    assert "employee personnel number (up to 7 digits)" in description


def test_success_posts_and_returns_payload(gs: Any, tmp_path: pathlib.Path) -> None:
    payload = {
        "documents": [
            {"id": "doc1", "title": "T", "content": "C", "url": "https://x.com", "scope": "web"},
        ],
        "search_metadata": {"status": "ok", "code": "200", "description": ""},
    }
    gs.shared_responses.extend([_GsResponse(payload)])

    result = json.loads(_call(gs, tmp_path, query="test"))

    assert result == payload
    client = gs.instances[-1]
    call = client.calls[0]
    assert call["url"] == "https://gigasearch.example/v3/search"
    assert call["json"] == {"query": "test", "limit": 10, "search_mode": "advanced"}
    assert client.kwargs["headers"]["X-Surface-ID"] == "test-surface"
    assert client.kwargs["headers"]["X-User-ID"] == "test-user"


def test_custom_limit(gs: Any, tmp_path: pathlib.Path) -> None:
    _call(gs, tmp_path, query="q", limit=7)

    assert gs.instances[-1].calls[0]["json"]["limit"] == 7


@pytest.mark.parametrize(
    ("query", "limit", "message"),
    [
        ("", 10, "query must not be empty"),
        ("x" * 2049, 10, "exceeds maximum length of 2048"),
        ("test", 0, "limit must be between 1 and 100"),
        ("test", 101, "limit must be between 1 and 100"),
    ],
)
def test_validation_errors_are_tool_failures(
    gs: Any,
    tmp_path: pathlib.Path,
    query: str,
    limit: int,
    message: str,
) -> None:
    result = _call(gs, tmp_path, query=query, limit=limit)

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert message in result
    assert not gs.instances or gs.instances[-1].calls == []


def test_non_json_http_error_is_bounded_tool_failure(gs: Any, tmp_path: pathlib.Path) -> None:
    gs.shared_responses.extend(
        [
            _GsResponse(_NON_JSON, status_code=502, text="<html>bad gateway</html>"),
        ]
    )

    result = _call(gs, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "HTTP 502" in result
    assert "bad gateway" in result


def test_non_json_success_is_tool_failure(gs: Any, tmp_path: pathlib.Path) -> None:
    gs.shared_responses.extend([_GsResponse(_NON_JSON, text="not-json")])

    result = _call(gs, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "invalid JSON response" in result


def test_request_error_is_tool_failure(
    gs: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _crash(*_args: Any, **_kwargs: Any) -> None:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(RecordingHttpClient, "post", _crash)

    result = _call(gs, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "connection refused" in result


@pytest.mark.parametrize("key", ["GIGASEARCH_CERT_PATH", "GIGASEARCH_KEY_PATH"])
def test_tls_client_rejects_incomplete_cert_pair(
    gs: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    monkeypatch.setenv(key, "/certs/value.pem")
    reset_memory_settings_for_tests()

    result = _call(gs, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "GIGASEARCH_CERT_PATH and GIGASEARCH_KEY_PATH must be set together" in result
    assert not gs.instances or gs.instances[-1].calls == []


def test_tls_client_uses_ca_and_client_cert_pair(
    gs: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIGASEARCH_CA_PATH", "/certs/ca.pem")
    monkeypatch.setenv("GIGASEARCH_CERT_PATH", "/certs/client.pem")
    monkeypatch.setenv("GIGASEARCH_KEY_PATH", "/certs/client.key")
    reset_memory_settings_for_tests()
    _call(gs, tmp_path, query="test")

    client_kwargs = gs.instances[-1].kwargs
    assert client_kwargs["verify"] == "/certs/ca.pem"
    assert client_kwargs["cert"] == ("/certs/client.pem", "/certs/client.key")


def test_tls_client_can_disable_server_verification(
    gs: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIGASEARCH_VERIFY_SSL", "false")
    reset_memory_settings_for_tests()

    _call(gs, tmp_path, query="test")

    assert gs.instances[-1].kwargs["verify"] is False


def test_trace_id_reaches_the_request_headers(gs: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ouroboros.observability import aef_tracing

    trace_id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    with aef_tracing.trace_scope(trace_id):
        _call(gs, tmp_path, query="test")

    assert gs.instances[-1].kwargs["headers"]["X-Trace-Id"] == "f47ac10b-58cc-4372-a567-0e02b2c3d479"


def test_no_trace_id_means_no_header(gs: Any, tmp_path: pathlib.Path) -> None:
    from ouroboros.observability import aef_tracing

    assert aef_tracing.current_trace_id() == ""
    _call(gs, tmp_path, query="test")

    assert "X-Trace-Id" not in gs.instances[-1].kwargs["headers"]


# --- unit tests for GigaSearch with DI ---


def test_gigasearch_unit_uses_injected_client_factory():
    """GigaSearch.execute uses the injected client_factory, not httpx.Client."""
    instances: list[RecordingHttpClient] = []

    def _factory(**kwargs: Any) -> RecordingHttpClient:
        client = RecordingHttpClient(**kwargs)
        client.responses = [_GsResponse({"documents": [], "search_metadata": {"status": "ok"}})]
        instances.append(client)
        return client

    gs_obj = gs_module.GigaSearch(
        base_url="https://gs.example",
        timeout=30.0,
        max_query_len=2048,
        max_limit=100,
        surface_id="s",
        user_id="u",
        client_factory=_factory,
    )

    result = json.loads(gs_obj.execute("query", 5, "advanced"))

    assert result["documents"] == []
    assert len(instances) == 1
    assert instances[0].calls[0]["url"] == "https://gs.example/v3/search"


def test_registry_exposes_core_parallel_gigasearch(tmp_path: pathlib.Path) -> None:
    from ouroboros.policy.capabilities import CORE_TOOL_NAMES
    from ouroboros.tools.catalog import spec_for_tool
    from tests.helpers.shared import TEST_SCOPE, make_tool_env

    catalog, _ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    spec = spec_for_tool("gigasearch")

    assert spec.surface == "core"
    assert spec.category == "network"
    assert spec.capability_class == "external_network"
    assert spec.parallel is True
    assert spec.result_limit == 30_000
    assert catalog.find("gigasearch", TEST_SCOPE).timeout_sec == 180
    assert "gigasearch" in catalog.local_entries()
    assert "gigasearch" in {
        schema.name for schema in [d.schema for d in catalog.descriptors(TEST_SCOPE) if d.name in CORE_TOOL_NAMES]
    }


def test_get_tools_returns_tool_entry_with_timeout(gs: Any) -> None:
    from new_core.tools.entry import ToolEntry

    tools = gs.module.get_tools()
    assert len(tools) == 1
    assert isinstance(tools[0], ToolEntry)
    assert tools[0].name == "gigasearch"
    assert tools[0].timeout_sec == 180
