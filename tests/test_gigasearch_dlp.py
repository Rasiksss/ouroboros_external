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
    load_settings,
    reset_memory_settings_for_tests,
    save_settings,
)
from tests.custom_parametrize import parametrize_from_dict

_NON_JSON = object()

SEARCH_PAYLOAD = {
    "documents": [{"id": "doc1", "title": "T", "content": "C", "url": "https://x.com", "scope": "web"}],
    "search_metadata": {"status": "ok", "code": "200", "description": ""},
}


class FakeResponse:
    def __init__(self, payload: Any = None, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

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

    def post(self, url: str, content: bytes | None = None, json: dict[str, Any] | None = None) -> FakeResponse:
        self.calls.append({"url": url, "content": content, "json": json, "client_kwargs": self.kwargs})
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(SEARCH_PAYLOAD)


def _dlp_ok() -> FakeResponse:
    return FakeResponse({"result": "ok", "vid": "vid-1"})


def _dlp_block(rule_names: list[str] | None = None) -> FakeResponse:
    matches = [{"rule": {"name": name}} for name in rule_names or []]
    return FakeResponse({"result": "block", "vid": "vid-1", "matches": matches})


@pytest.fixture()
def dlp(monkeypatch: pytest.MonkeyPatch):
    FakeClient.calls = []
    FakeClient.responses = []
    monkeypatch.setenv("GIGASEARCH_BASE_URL", "https://gigasearch.example")
    monkeypatch.setenv("GIGASEARCH_SURFACE_ID", "test-surface")
    monkeypatch.setenv("GIGASEARCH_USER_ID", "test-user")
    monkeypatch.setenv("DLP_BASE_URL", "http://dlp.example:3010")
    monkeypatch.setenv("DLP_SENDER_EMAIL", "tuz@sberbank.ru")
    monkeypatch.setattr(httpx, "Client", FakeClient)
    reset_memory_settings_for_tests()
    yield gs_module
    reset_memory_settings_for_tests()


def _call(gs: Any, tmp_path: pathlib.Path, **kwargs: Any) -> str:
    ctx = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path / "data")
    return gs._gigasearch_handler(ctx, **kwargs)


def test_ok_verdict_proceeds_to_search(dlp: Any, tmp_path: pathlib.Path) -> None:
    FakeClient.responses = [_dlp_ok(), FakeResponse(SEARCH_PAYLOAD)]

    result = json.loads(_call(dlp, tmp_path, query="safe question"))

    assert result == SEARCH_PAYLOAD
    assert [call["url"] for call in FakeClient.calls] == [
        "http://dlp.example:3010/filter",
        "https://gigasearch.example/v3/search",
    ]
    assert FakeClient.calls[0]["content"] == b"safe question"


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
    FakeClient.responses = [_dlp_ok(), FakeResponse(SEARCH_PAYLOAD)]

    _call(dlp, tmp_path, query="safe question")

    headers = FakeClient.calls[0]["client_kwargs"]["headers"]
    assert headers[header] == value


def test_check_sends_fresh_message_metadata(dlp: Any, tmp_path: pathlib.Path) -> None:
    FakeClient.responses = [_dlp_ok(), FakeResponse(SEARCH_PAYLOAD)]

    _call(dlp, tmp_path, query="safe question")

    headers = FakeClient.calls[0]["client_kwargs"]["headers"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", headers["X-Message-Date"])
    assert 0 < len(headers["X-Message-ID"]) <= 1000


def test_empty_setting_omits_its_headers(dlp: Any) -> None:
    FakeClient.responses = [_dlp_ok()]
    client = dlp.DlpClient(
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

    headers = FakeClient.calls[0]["client_kwargs"]["headers"]
    assert "X-Dozor-Mapping-Schema" not in headers
    assert "X-Application" not in headers
    assert "X-Recipient-User-Email" not in headers
    assert "X-Server-IP" not in headers
    assert "X-Client-IP" not in headers
    assert "X-Subject" not in headers
    assert "Content-Type" not in headers
    assert "Allow" not in headers
    assert headers["X-Sender-User-Email"] == "tuz@sberbank.ru"


def test_timeout_comes_from_settings(
    dlp: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DLP_TIMEOUT_SEC", "5")
    reset_memory_settings_for_tests()
    FakeClient.responses = [_dlp_ok(), FakeResponse(SEARCH_PAYLOAD)]

    _call(dlp, tmp_path, query="safe question")

    assert FakeClient.calls[0]["client_kwargs"]["timeout"] == 5.0


def test_no_content_verdict_proceeds_to_search(dlp: Any, tmp_path: pathlib.Path) -> None:
    FakeClient.responses = [FakeResponse(_NON_JSON, status_code=204), FakeResponse(SEARCH_PAYLOAD)]

    result = json.loads(_call(dlp, tmp_path, query="safe question"))

    assert result == SEARCH_PAYLOAD
    assert [call["url"] for call in FakeClient.calls] == [
        "http://dlp.example:3010/filter",
        "https://gigasearch.example/v3/search",
    ]


def test_forbidden_response_is_a_block_verdict(dlp: Any, tmp_path: pathlib.Path) -> None:
    FakeClient.responses = [FakeResponse(_NON_JSON, status_code=403)]

    result = _call(dlp, tmp_path, query="secret data")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "blocked by DLP" in result
    assert len(FakeClient.calls) == 1


def test_block_verdict_is_final_for_the_model(dlp: Any, tmp_path: pathlib.Path) -> None:
    FakeClient.responses = [_dlp_block(["#policy.rule.storage"])]

    result = _call(dlp, tmp_path, query="secret data")

    assert "the verdict is final: do not retry, rephrase or split the query" in result
    assert "tell the user this cannot be searched" in result


def test_block_verdict_fails_without_search_call(dlp: Any, tmp_path: pathlib.Path) -> None:
    FakeClient.responses = [_dlp_block(["#policy.rule.storage"])]

    result = _call(dlp, tmp_path, query="secret data")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "blocked by DLP" in result
    assert "#policy.rule.storage" in result
    assert len(FakeClient.calls) == 1


def test_block_verdict_without_rule_names(dlp: Any, tmp_path: pathlib.Path) -> None:
    FakeClient.responses = [_dlp_block()]

    result = _call(dlp, tmp_path, query="secret data")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "blocked by DLP" in result
    assert len(FakeClient.calls) == 1


@parametrize_from_dict(
    {
        "bad request headers": {"status_code": 400},
        "queue timeout": {"status_code": 408},
        "server error": {"status_code": 500},
    }
)
def test_dlp_http_error_fails_without_search_call(
    dlp: Any,
    tmp_path: pathlib.Path,
    status_code: int,
) -> None:
    FakeClient.responses = [FakeResponse(_NON_JSON, status_code=status_code)]

    result = _call(dlp, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert f"HTTP {status_code}" in result
    assert len(FakeClient.calls) == 1


def test_dlp_connection_error_fails_without_search_call(
    dlp: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _crash(*_args: Any, **_kwargs: Any) -> None:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(FakeClient, "post", _crash)

    result = _call(dlp, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "connection refused" in result


def test_unexpected_verdict_fails_without_search_call(dlp: Any, tmp_path: pathlib.Path) -> None:
    FakeClient.responses = [FakeResponse({"result": "quarantine"})]

    result = _call(dlp, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "quarantine" in result
    assert len(FakeClient.calls) == 1


def test_non_json_verdict_fails_without_search_call(dlp: Any, tmp_path: pathlib.Path) -> None:
    FakeClient.responses = [FakeResponse(_NON_JSON)]

    result = _call(dlp, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "invalid JSON" in result
    assert len(FakeClient.calls) == 1


def test_empty_dlp_base_url_refuses_search(dlp: Any, tmp_path: pathlib.Path) -> None:
    save_settings(load_settings().model_copy(update={"DLP_BASE_URL": ""}))
    FakeClient.responses = [FakeResponse(SEARCH_PAYLOAD)]

    result = _call(dlp, tmp_path, query="test")

    assert result.startswith("⚠️ TOOL_ERROR (gigasearch):")
    assert "DLP is not configured" in result
    assert "DLP_BASE_URL" in result
    assert FakeClient.calls == []
