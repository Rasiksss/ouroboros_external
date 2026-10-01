from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import ouroboros.tools.gigasearch as gs_module
from new_core.tools.entry import ToolEntry
from ouroboros.policy.capabilities import CORE_TOOL_NAMES
from ouroboros.storage.settings.store import reset_memory_settings_for_tests
from ouroboros.tools.catalog import spec_for_tool
from tests._shared import make_tool_env

_NON_JSON = object()


class FakeResponse:
    def __init__(
        self,
        payload: Any = None,
        *,
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self.payload = (
            payload
            if payload is not None
            else {
                "documents": [],
                "search_metadata": {"status": "ok", "code": "200", "description": ""},
            }
        )
        self.status_code = status_code
        self.text = text

    @property
    def is_success(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        if self.payload is _NON_JSON:
            raise ValueError("response is not JSON")
        return self.payload


class FakeClient:
    calls: list[dict[str, Any]] = []
    responses: list[FakeResponse] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
        self.calls.append({"url": url, "json": json, "client_kwargs": self.kwargs})
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse()


@pytest.fixture()
def gs(monkeypatch: pytest.MonkeyPatch):
    FakeClient.calls = []
    FakeClient.responses = []
    monkeypatch.setenv("GIGASEARCH_BASE_URL", "https://gigasearch.example")
    monkeypatch.setenv("GIGASEARCH_SURFACE_ID", "test-surface")
    monkeypatch.setenv("GIGASEARCH_USER_ID", "test-user")
    # The gate is mandatory but owns its own test module; here it is configured and stubbed out.
    monkeypatch.setenv("DLP_BASE_URL", "http://dlp.example:3010")
    monkeypatch.setattr(gs_module.DlpClient, "check", lambda _self, _query: None)
    monkeypatch.setattr(httpx, "Client", FakeClient)
    reset_memory_settings_for_tests()
    yield gs_module
    reset_memory_settings_for_tests()


def _ctx(tmp_path: pathlib.Path) -> SimpleNamespace:
    return SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path / "data")


def _call(gs: Any, tmp_path: pathlib.Path, **kwargs: Any) -> str:
    return gs._gigasearch_handler(_ctx(tmp_path), **kwargs)


def test_registry_exposes_core_parallel_gigasearch(tmp_path: pathlib.Path) -> None:
    catalog, _ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    spec = spec_for_tool("gigasearch")

    assert spec.surface == "core"
    assert spec.category == "network"
    assert spec.capability_class == "external_network"
    assert spec.parallel is True
    assert spec.result_limit == 30_000
    assert catalog.find("gigasearch").timeout_sec == 180
    assert "gigasearch" in {
        schema.name for schema in [d.schema for d in catalog.descriptors() if d.name in CORE_TOOL_NAMES]
    }


def test_schema_shape(gs: Any) -> None:
    schema = gs.get_tools()[0].schema

    assert schema.name == "gigasearch"
    assert schema.parameters["required"] == ["query"]
    assert schema.parameters["properties"]["query"]["type"] == "string"
    assert schema.parameters["properties"]["limit"]["type"] == "integer"


def test_success_posts_and_returns_payload(gs: Any, tmp_path: pathlib.Path) -> None:
    payload = {
        "documents": [
            {"id": "doc1", "title": "T", "content": "C", "url": "https://x.com", "scope": "web"},
        ],
        "search_metadata": {"status": "ok", "code": "200", "description": ""},
    }
    FakeClient.responses = [FakeResponse(payload)]

    result = json.loads(_call(gs, tmp_path, query="test"))

    assert result == payload
    call = FakeClient.calls[0]
    assert call["url"] == "https://gigasearch.example/v3/search"
    assert call["json"] == {"query": "test", "limit": 10, "search_mode": "advanced"}
    assert call["client_kwargs"]["headers"]["X-Surface-ID"] == "test-surface"
    assert call["client_kwargs"]["headers"]["X-User-ID"] == "test-user"


def test_custom_limit(gs: Any, tmp_path: pathlib.Path) -> None:
    _call(gs, tmp_path, query="q", limit=7)

    assert FakeClient.calls[0]["json"]["limit"] == 7


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
    assert FakeClient.calls == []


def test_non_json_http_error_is_bounded_tool_failure(gs: Any, tmp_path: pathlib.Path) -> None:
    FakeClient.responses = [
        FakeResponse(_NON_JSON, status_code=502, text="<html>bad gateway</html>"),
    ]

    result = _call(gs, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "HTTP 502" in result
    assert "bad gateway" in result


def test_non_json_success_is_tool_failure(gs: Any, tmp_path: pathlib.Path) -> None:
    FakeClient.responses = [FakeResponse(_NON_JSON, text="not-json")]

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

    monkeypatch.setattr(FakeClient, "post", _crash)

    result = _call(gs, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "connection refused" in result


def test_tls_client_uses_ca_and_client_cert_pair(
    gs: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIGASEARCH_CA_PATH", "/certs/ca.pem")
    monkeypatch.setenv("GIGASEARCH_CERT_PATH", "/certs/client.pem")
    monkeypatch.setenv("GIGASEARCH_KEY_PATH", "/certs/client.key")
    reset_memory_settings_for_tests()

    _call(gs, tmp_path, query="test")

    client_kwargs = FakeClient.calls[0]["client_kwargs"]
    assert client_kwargs["verify"] == "/certs/ca.pem"
    assert client_kwargs["cert"] == ("/certs/client.pem", "/certs/client.key")


def test_tls_client_can_disable_server_verification(
    gs: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIGASEARCH_VERIFY_SSL", "false")
    reset_memory_settings_for_tests()

    _call(gs, tmp_path, query="test")

    assert FakeClient.calls[0]["client_kwargs"]["verify"] is False


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
    assert FakeClient.calls == []


def test_registry(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIGASEARCH_SURFACE_ID", "surface")
    monkeypatch.setenv("GIGASEARCH_USER_ID", "user")

    catalog, _ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")

    spec = spec_for_tool("gigasearch")
    assert spec.category == "network"
    assert spec.capability_class == "external_network"

    assert catalog.find("gigasearch").timeout_sec == 180
    assert "gigasearch" in catalog.local_entries()


def test_get_tools(gs: Any) -> None:

    tools = gs.get_tools()
    assert len(tools) == 1
    assert isinstance(tools[0], ToolEntry)
    assert tools[0].name == "gigasearch"
    assert tools[0].timeout_sec == 180
