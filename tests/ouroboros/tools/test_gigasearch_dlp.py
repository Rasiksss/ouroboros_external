from __future__ import annotations

import json
import pathlib
import re
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import ouroboros.tools.gigasearch as gs_module
from ouroboros.storage.settings.store import (
    reset_memory_settings_for_tests,
)
from tests.helpers.http_double import FakeHttpResponse, RecordingHttpClient
from tests.helpers.parametrize import parametrize_from_dict

# Headers omitted by test_empty_setting_omits_its_headers — every one must be absent.
_OMITTABLE_HEADERS = [
    "X-Dozor-Mapping-Schema",
    "X-Application",
    "X-Recipient-User-Email",
    "X-Server-IP",
    "X-Client-IP",
    "X-Subject",
    "Content-Type",
    "Allow",
]

_NON_JSON = object()

SEARCH_PAYLOAD = {
    "documents": [{"id": "doc1", "title": "T", "content": "C", "url": "https://x.com", "scope": "web"}],
    "search_metadata": {"status": "ok", "code": "200", "description": ""},
}


class _DlpResponse(FakeHttpResponse):
    """Extends FakeHttpResponse with the _NON_JSON sentinel and default payload."""

    def __init__(self, payload: Any = None, *, status_code: int = 200) -> None:
        if payload is _NON_JSON:
            self._raise_on_json = True
            payload = None
        else:
            self._raise_on_json = False
        super().__init__(payload if payload is not None else SEARCH_PAYLOAD, status_code=status_code)

    def json(self) -> Any:
        if self._raise_on_json:
            raise ValueError("response is not JSON")
        return super().json()


def _dlp_ok() -> _DlpResponse:
    return _DlpResponse({"result": "ok", "vid": "vid-1"})


def _dlp_block(rule_names: list[str] | None = None) -> _DlpResponse:
    matches = [{"rule": {"name": name}} for name in rule_names or []]
    return _DlpResponse({"result": "block", "vid": "vid-1", "matches": matches})


@pytest.fixture()
def dlp(monkeypatch: pytest.MonkeyPatch):
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
    monkeypatch.setenv("DLP_SENDER_EMAIL", "tuz@sberbank.ru")
    monkeypatch.setattr(httpx, "Client", _factory)
    reset_memory_settings_for_tests()
    yield SimpleNamespace(
        module=gs_module,
        instances=instances,
        shared_responses=shared_responses,
    )
    reset_memory_settings_for_tests()


def _call(dlp: Any, tmp_path: pathlib.Path, **kwargs: Any) -> str:
    ctx = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path / "data")
    return dlp.module._gigasearch_handler(ctx, **kwargs)


def test_ok_verdict_proceeds_to_search(dlp: Any, tmp_path: pathlib.Path) -> None:
    dlp.shared_responses.extend([_dlp_ok(), _DlpResponse(SEARCH_PAYLOAD)])

    result = json.loads(_call(dlp, tmp_path, query="safe question"))

    assert result == SEARCH_PAYLOAD
    all_calls = [call for inst in dlp.instances for call in inst.calls]
    assert [call["url"] for call in all_calls] == [
        "http://dlp.example:3010/filter",
        "https://gigasearch.example/v3/search",
    ]
    assert all_calls[0]["content"] == b"safe question"


@parametrize_from_dict(
    {
        "content type": {"header": "Content-Type", "value": "application/octet-stream"},
        "content is not returned back": {"header": "Allow", "value": "204"},
        "mapping schema": {"header": "X-Dozor-Mapping-Schema", "value": "orion"},
        "application equals mapping schema": {"header": "X-Application", "value": "orion"},
        "sender is the technical account": {
            "header": "X-Sender-User-Email",
            "value": "tuz@sberbank.ru",
        },
        "recipient group": {"header": "X-Recipient-User-Email", "value": "IHAPI-Internal"},
        "server ip stub": {"header": "X-Server-IP", "value": "1.1.1.1"},
        "client ip stub": {"header": "X-Client-IP", "value": "1.1.1.1"},
        "base64 subject": {
            "header": "X-Subject",
            "value": "=?UTF-8?B?0J/QvtC40YHQuiDQsiDQuNC90YLQtdGA0L3QtdGC0LU=?=",
        },
    }
)
def test_check_sends_agreed_dozor_headers(
    dlp: Any,
    tmp_path: pathlib.Path,
    header: str,
    value: str,
) -> None:
    dlp.shared_responses.extend([_dlp_ok(), _DlpResponse(SEARCH_PAYLOAD)])

    _call(dlp, tmp_path, query="safe question")

    headers = dlp.instances[0].kwargs["headers"]
    assert headers[header] == value


def test_check_sends_fresh_message_metadata(dlp: Any, tmp_path: pathlib.Path) -> None:
    dlp.shared_responses.extend([_dlp_ok(), _DlpResponse(SEARCH_PAYLOAD)])

    _call(dlp, tmp_path, query="safe question")

    headers = dlp.instances[0].kwargs["headers"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", headers["X-Message-Date"])
    assert 0 < len(headers["X-Message-ID"]) <= 1000


def test_empty_setting_omits_its_headers(dlp: Any) -> None:
    dlp.shared_responses.extend([_dlp_ok()])
    client = dlp.module.DlpClient(
        base_url="http://dlp.example:3010",
        mapping_schema="",
        sender_email="tuz@sberbank.ru",
        recipient_email="",
        server_ip="",
        client_ip="",
        subject="",
        content_type="",
        allow="",
        timeout=30.0,
    )

    client.check("safe question")

    headers = dlp.instances[0].kwargs["headers"]
    for name in _OMITTABLE_HEADERS:
        assert name not in headers
    assert headers["X-Sender-User-Email"] == "tuz@sberbank.ru"


def test_no_content_verdict_proceeds_to_search(dlp: Any, tmp_path: pathlib.Path) -> None:
    dlp.shared_responses.extend([_DlpResponse(status_code=204), _DlpResponse(SEARCH_PAYLOAD)])

    result = json.loads(_call(dlp, tmp_path, query="safe question"))

    assert result == SEARCH_PAYLOAD
    all_calls = [call for inst in dlp.instances for call in inst.calls]
    assert [call["url"] for call in all_calls] == [
        "http://dlp.example:3010/filter",
        "https://gigasearch.example/v3/search",
    ]


def test_block_verdict_fails_without_search_call(dlp: Any, tmp_path: pathlib.Path) -> None:
    dlp.shared_responses.extend([_dlp_block(["#policy.rule.storage"])])

    result = _call(dlp, tmp_path, query="classified")

    assert "⚠️ TOOL_ERROR (gigasearch):" in result
    assert "blocked by DLP" in result
    assert "policy.rule.storage" in result
    assert "the verdict is final: do not retry, rephrase or split the query" in result
    assert "tell the user this cannot be searched" in result
    assert len(dlp.instances[0].calls) == 1


def test_block_verdict_with_multiple_rules(dlp: Any, tmp_path: pathlib.Path) -> None:
    dlp.shared_responses.extend([_dlp_block(["#policy.rule.storage", "#policy.rule.pii"])])

    result = _call(dlp, tmp_path, query="classified")

    assert "policy.rule.storage" in result
    assert "policy.rule.pii" in result


def test_block_verdict_without_rules(dlp: Any, tmp_path: pathlib.Path) -> None:
    dlp.shared_responses.extend([_dlp_block()])

    result = _call(dlp, tmp_path, query="classified")

    assert "⚠️ TOOL_ERROR (gigasearch):" in result
    assert "blocked by DLP" in result
    assert len(dlp.instances[0].calls) == 1


def test_forbidden_response_is_a_block_verdict(dlp: Any, tmp_path: pathlib.Path) -> None:
    dlp.shared_responses.extend([_DlpResponse(_NON_JSON, status_code=403)])

    result = _call(dlp, tmp_path, query="secret data")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "blocked by DLP" in result
    assert len(dlp.instances[0].calls) == 1


@parametrize_from_dict(
    {
        "bad request": {"status_code": 400},
        "queue timeout": {"status_code": 408},
        "server error": {"status_code": 500},
    }
)
def test_dlp_http_error_fails_without_search_call(dlp: Any, tmp_path: pathlib.Path, status_code: int) -> None:
    dlp.shared_responses.extend([_DlpResponse(_NON_JSON, status_code=status_code)])

    result = _call(dlp, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert f"HTTP {status_code}" in result
    assert len(dlp.instances[0].calls) == 1


def test_quarantine_verdict_fails_without_search_call(dlp: Any, tmp_path: pathlib.Path) -> None:
    dlp.shared_responses.extend([_DlpResponse({"result": "quarantine"})])

    result = _call(dlp, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "unexpected DLP verdict: 'quarantine'" in result
    assert len(dlp.instances[0].calls) == 1


def test_non_json_verdict_fails(dlp: Any, tmp_path: pathlib.Path) -> None:
    dlp.shared_responses.extend([_DlpResponse(_NON_JSON)])

    result = _call(dlp, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "invalid JSON" in result
    assert len(dlp.instances[0].calls) == 1


def test_dlp_connection_error_fails_without_search_call(
    dlp: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _crash(*_args: Any, **_kwargs: Any) -> None:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(RecordingHttpClient, "post", _crash)

    result = _call(dlp, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "connection refused" in result
    assert len(dlp.instances) == 1


def test_empty_dlp_base_url_refuses_search(
    dlp: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DLP_BASE_URL", "")
    reset_memory_settings_for_tests()

    result = _call(dlp, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "DLP is not configured" in result
    assert "DLP_BASE_URL" in result
    assert len(dlp.instances) == 0


def test_timeout_comes_from_settings(
    dlp: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DLP_TIMEOUT_SEC", "5")
    reset_memory_settings_for_tests()
    dlp.shared_responses.extend([_dlp_ok(), _DlpResponse(SEARCH_PAYLOAD)])

    _call(dlp, tmp_path, query="safe question")

    assert dlp.instances[0].kwargs["timeout"] == 5.0


def test_the_dlp_check_carries_the_active_operation_identifier(dlp: Any, tmp_path: pathlib.Path) -> None:
    from ouroboros.observability import aef_tracing

    dlp.shared_responses.extend([_dlp_ok(), _DlpResponse(SEARCH_PAYLOAD)])

    with aef_tracing.trace_scope("f47ac10b-58cc-4372-a567-0e02b2c3d479"):
        _call(dlp, tmp_path, query="safe question")

    assert dlp.instances[0].kwargs["headers"]["X-Trace-Id"] == "f47ac10b-58cc-4372-a567-0e02b2c3d479"


def test_the_dlp_check_omits_the_header_outside_an_operation(dlp: Any, tmp_path: pathlib.Path) -> None:
    """Dozor rejects an empty header value, so an absent identifier means an absent header."""
    from ouroboros.observability import aef_tracing

    assert aef_tracing.current_trace_id() == ""
    dlp.shared_responses.extend([_dlp_ok(), _DlpResponse(SEARCH_PAYLOAD)])

    _call(dlp, tmp_path, query="safe question")

    assert "X-Trace-Id" not in dlp.instances[0].kwargs["headers"]


# --- unit tests for DlpClient with DI ---


def test_dlp_client_unit_uses_injected_client_factory():
    """DlpClient.check uses the injected client_factory, not httpx.Client."""
    instances: list[RecordingHttpClient] = []

    def _factory(**kwargs: Any) -> RecordingHttpClient:
        client = RecordingHttpClient(**kwargs)
        client.responses = [_DlpResponse({"result": "ok", "vid": "vid-1"})]
        instances.append(client)
        return client

    dlp_client = gs_module.DlpClient(
        base_url="http://dlp.example:3010",
        mapping_schema="orion",
        sender_email="tuz@sberbank.ru",
        recipient_email="support@x.example",
        server_ip="10.0.0.1",
        client_ip="10.0.0.2",
        subject="test",
        content_type="application/octet-stream",
        allow="204",
        timeout=30.0,
        client_factory=_factory,
    )

    dlp_client.check("safe query")

    assert len(instances) == 1
    assert instances[0].calls[0]["url"] == "http://dlp.example:3010/filter"
    assert instances[0].calls[0]["content"] == b"safe query"
