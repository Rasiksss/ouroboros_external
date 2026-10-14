from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ouroboros.egress.workstation.client import (
    ErrorCode,
    SessionStatus,
    WorkstationApiError,
    WorkstationClient,
    WorkstationConfig,
    WorkstationProtocolError,
    WorkstationTransportError,
)
from ouroboros.observability import aef_tracing


def _config(**overrides: Any) -> WorkstationConfig:
    base = {
        "base_url": "https://ws.example",
        "create_secret": "",
        "session_strategy": "ephemeral",
        "idle_timeout_sec": 900,
        "absolute_ttl_sec": 3600,
        "command_timeout_sec": 300,
        "http_timeout_sec": 30.0,
        "max_attempts": 3,
        "retry_base_delay_sec": 0.1,
        "workspace_root": "/workspace",
        "access_level": "rw",
        "attachment_max_bytes": 1024,
    }
    base.update(overrides)
    return WorkstationConfig(**base)


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, *, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FakeStreamResponse:
    def __init__(self, status_code: int, lines: list[str], *, error_payload: Any = None) -> None:
        self.status_code = status_code
        self._lines = lines
        self._error_payload = error_payload or {"code": "internal", "message": "stream failed"}

    def __enter__(self) -> FakeStreamResponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def iter_lines(self):
        yield from self._lines

    def read(self) -> None:
        return None

    def json(self) -> Any:
        return self._error_payload


class FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Any] = []
        self.stream_responses: list[FakeStreamResponse] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def stream(self, method: str, url: str, **kwargs: Any) -> FakeStreamResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.stream_responses.pop(0)

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fake_client(monkeypatch: pytest.MonkeyPatch):
    instances: list[FakeClient] = []

    def _factory(**kwargs: Any) -> FakeClient:
        client = FakeClient(**kwargs)
        instances.append(client)
        return client

    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    return _factory, instances


def test_4xx_is_raised_immediately_without_retry(fake_client) -> None:
    client = WorkstationClient(_config(), client_factory=fake_client[0])
    client._get_client()  # force the lazy singleton to materialize so responses can be queued
    inner = fake_client[1][-1]
    inner.responses = [FakeResponse(404, {"code": "not-found", "message": "no such session"})]

    with pytest.raises(WorkstationApiError) as exc_info:
        client.get_session("sess-1", "tok")

    assert exc_info.value.is_not_found
    assert exc_info.value.status_code == 404
    assert len(inner.calls) == 1


def test_5xx_retries_up_to_max_attempts_then_raises(fake_client) -> None:
    client = WorkstationClient(_config(max_attempts=3), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.responses = [
        FakeResponse(500, {"code": "internal", "message": "boom"}),
        FakeResponse(502, {"code": "internal", "message": "boom2"}),
        FakeResponse(503, {"code": "internal", "message": "boom3"}),
    ]

    with pytest.raises(WorkstationApiError) as exc_info:
        client.get_session("sess-1", "tok")

    assert exc_info.value.status_code == 503
    assert len(inner.calls) == 3


def test_transport_failure_retries_then_raises_transport_error(fake_client) -> None:
    client = WorkstationClient(_config(max_attempts=2), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.responses = [httpx.ConnectError("boom"), httpx.ConnectError("boom again")]

    with pytest.raises(WorkstationTransportError):
        client.get_session("sess-1", "tok")

    assert len(inner.calls) == 2


def test_success_after_transient_5xx_returns_without_further_retry(fake_client) -> None:
    client = WorkstationClient(_config(max_attempts=3), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.responses = [
        FakeResponse(500, {"code": "internal", "message": "boom"}),
        FakeResponse(200, {"id": "sess-1", "status": "ready", "quotas": {}}),
    ]

    state = client.get_session("sess-1", "tok")

    assert state.status == SessionStatus.READY
    assert len(inner.calls) == 2


def test_create_session_header_matrix(fake_client) -> None:
    client = WorkstationClient(_config(create_secret="s3cr3t"), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.responses = [FakeResponse(201, {"id": "sess-1", "status": "ready", "token": "tok-1"})]

    result = client.create_session("sess-1")

    assert result.created is True
    assert result.token == "tok-1"
    headers = inner.calls[0]["headers"]
    assert headers["X-Workstation-Protocol"] == "1"
    assert headers["X-Session-Id"] == "sess-1"
    assert headers["X-Session-Create-Secret"] == "s3cr3t"
    assert "Authorization" not in headers


def test_create_session_without_secret_omits_header(fake_client) -> None:
    client = WorkstationClient(_config(create_secret=""), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.responses = [FakeResponse(200, {"id": "sess-1", "status": "ready", "token": "tok-1"})]

    result = client.create_session("sess-1")

    assert result.created is False
    headers = inner.calls[0]["headers"]
    assert "X-Session-Create-Secret" not in headers


def test_non_create_route_sends_bearer_never_create_secret(fake_client) -> None:
    client = WorkstationClient(_config(create_secret="s3cr3t"), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.responses = [FakeResponse(200, {"id": "sess-1", "status": "ready", "quotas": {}})]

    client.get_session("sess-1", "tok-1")

    headers = inner.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer tok-1"
    assert "X-Session-Create-Secret" not in headers


def test_create_session_without_token_is_protocol_error(fake_client) -> None:
    client = WorkstationClient(_config(), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.responses = [FakeResponse(201, {"id": "sess-1", "status": "ready"})]

    with pytest.raises(WorkstationProtocolError):
        client.create_session("sess-1")


def test_upload_file_sends_multipart_fields(fake_client) -> None:
    client = WorkstationClient(_config(), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.responses = [FakeResponse(200, {"path": "media/inbound/a.txt", "bytes": 3, "sha256": "abc"})]

    result = client.upload_file(
        session_id="sess-1", token="tok", path="media/inbound/a.txt", data=b"abc", filename="a.txt", mime="text/plain"
    )

    assert result.bytes_written == 3
    call = inner.calls[0]
    assert call["data"] == {"path": "media/inbound/a.txt"}
    assert call["files"]["file"] == ("a.txt", b"abc", "text/plain")


def test_exec_stream_accumulates_events_and_requires_exit(fake_client) -> None:
    client = WorkstationClient(_config(), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.stream_responses = [
        FakeStreamResponse(
            200,
            [
                json.dumps({"kind": "stdout", "data": "hello"}),
                "",
                json.dumps({"kind": "stderr", "data": "oops"}),
                json.dumps({"kind": "exit", "exitCode": 0, "timedOut": False}),
            ],
        )
    ]
    events: list[dict[str, Any]] = []

    client.exec_stream(
        session_id="sess-1",
        token="tok",
        command=["/bin/sh", "-c", "echo hi"],
        cwd="/workspace",
        env=None,
        stdin=None,
        timeout_sec=30,
        on_event=events.append,
    )

    assert [e["kind"] for e in events] == ["stdout", "stderr", "exit"]
    # Single attempt: streaming never retries.
    assert len(inner.calls) == 1


def test_exec_stream_missing_exit_record_is_protocol_error(fake_client) -> None:
    client = WorkstationClient(_config(), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.stream_responses = [FakeStreamResponse(200, [json.dumps({"kind": "stdout", "data": "hi"})])]

    with pytest.raises(WorkstationProtocolError):
        client.exec_stream(
            session_id="sess-1",
            token="tok",
            command=["x"],
            cwd="/workspace",
            env=None,
            stdin=None,
            timeout_sec=10,
            on_event=lambda _e: None,
        )


def test_exec_stream_non_json_line_is_protocol_error(fake_client) -> None:
    client = WorkstationClient(_config(), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.stream_responses = [FakeStreamResponse(200, ["not json"])]

    with pytest.raises(WorkstationProtocolError):
        client.exec_stream(
            session_id="sess-1",
            token="tok",
            command=["x"],
            cwd="/workspace",
            env=None,
            stdin=None,
            timeout_sec=10,
            on_event=lambda _e: None,
        )


def test_exec_stream_error_status_is_not_retried(fake_client) -> None:
    client = WorkstationClient(_config(max_attempts=3), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.stream_responses = [FakeStreamResponse(500, [])]

    with pytest.raises(WorkstationApiError):
        client.exec_stream(
            session_id="sess-1",
            token="tok",
            command=["x"],
            cwd="/workspace",
            env=None,
            stdin=None,
            timeout_sec=10,
            on_event=lambda _e: None,
        )

    assert len(inner.calls) == 1


def test_config_and_secret_not_in_repr() -> None:
    config = _config(create_secret="s3cr3t")
    assert "s3cr3t" not in repr(config)


def test_from_settings_returns_none_when_base_url_empty() -> None:
    from ouroboros.storage.settings.schema import AppSettings

    settings = AppSettings(WORKSTATION_BASE_URL="")
    assert WorkstationConfig.from_settings(settings) is None


def test_from_settings_builds_config_from_app_settings() -> None:
    from ouroboros.storage.settings.schema import AppSettings

    settings = AppSettings(
        WORKSTATION_BASE_URL="https://ws.example/",
        WORKSTATION_CREATE_SECRET="s3cr3t",
        WORKSTATION_RETRY_BASE_DELAY_MS="250",
    )
    config = WorkstationConfig.from_settings(settings)
    assert config is not None
    assert config.base_url == "https://ws.example"
    assert config.create_secret == "s3cr3t"
    assert config.retry_base_delay_sec == pytest.approx(0.25)


def test_unrecognized_session_status_is_a_protocol_error(fake_client) -> None:
    """`SessionStatus` is a genuinely closed set (spec §3.1, no "other" row) —
    an unrecognized value from the wire is a contract violation, not tolerated."""
    client = WorkstationClient(_config(), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.responses = [FakeResponse(200, {"id": "sess-1", "status": "quantum-superposition", "quotas": {}})]

    with pytest.raises(WorkstationProtocolError):
        client.get_session("sess-1", "tok")


def test_unrecognized_error_code_falls_back_to_internal(fake_client) -> None:
    """`code` (spec §7.1) has an explicit "прочее/internal" row — unlike session
    status, an unrecognized value is defined to mean `internal`, not a violation."""
    client = WorkstationClient(_config(), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.responses = [FakeResponse(404, {"code": "some-future-code", "message": "??"})]

    with pytest.raises(WorkstationApiError) as exc_info:
        client.get_session("sess-1", "tok")

    assert exc_info.value.code == ErrorCode.INTERNAL


_FIRST_TRACE = "1d5f9a6c-6b7a-4a2e-9d0a-9f7c1b2e3d40"
_SECOND_TRACE = "2e6a0b7d-7c8b-4b3f-8e1b-0a8d2c3f4e51"


@pytest.fixture()
def agent_id(monkeypatch: pytest.MonkeyPatch):
    """`current_agent_id` reads the settings store per call, through a function-local import."""
    from ouroboros.storage.settings import store

    def _set(value: str) -> None:
        monkeypatch.setattr(
            store,
            "load_settings",
            lambda: SimpleNamespace(AGENT_SYSTEM_CONFIGURATION_AGENT_ID=value, AGENT_ID=""),
        )

    _set("CI0000123")
    return _set


def test_two_operations_over_one_client_each_send_their_own_identifiers(fake_client, agent_id) -> None:
    """`X-Trace-Id` says which operation and `X-Agent-Id` which agent. The client is cached for the
    life of the process, so both have to be read at send time: a pair merged in at construction
    would attribute the second operation to the first, which looks right in a trace viewer."""
    client = WorkstationClient(_config(), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.responses = [
        FakeResponse(200, {"id": "sess-1", "status": "ready", "quotas": {}}),
        FakeResponse(200, {"id": "sess-1", "status": "ready", "quotas": {}}),
    ]

    for trace_id in (_FIRST_TRACE, _SECOND_TRACE):
        with aef_tracing.trace_scope(trace_id):
            client.get_session("sess-1", "tok")

    sent = [
        (call["headers"].get(aef_tracing.TRACE_HEADER_NAME), call["headers"].get(aef_tracing.AGENT_HEADER_NAME))
        for call in inner.calls
    ]
    assert sent == [(_FIRST_TRACE, "CI0000123"), (_SECOND_TRACE, "CI0000123")]


def test_the_streamed_exec_route_carries_the_identifiers_too(fake_client, agent_id) -> None:
    """`exec:stream` builds its request outside `_send`, so it is the one route that can lose the
    headers without any other test noticing."""
    client = WorkstationClient(_config(), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.stream_responses = [FakeStreamResponse(200, [json.dumps({"kind": "exit", "exitCode": 0})])]

    with aef_tracing.trace_scope(_FIRST_TRACE):
        client.exec_stream(
            session_id="sess-1",
            token="tok",
            command=["/bin/sh", "-c", "echo hi"],
            cwd="/workspace",
            env=None,
            stdin=None,
            timeout_sec=5,
            on_event=lambda _event: None,
        )

    headers = inner.calls[0]["headers"]
    assert headers[aef_tracing.TRACE_HEADER_NAME] == _FIRST_TRACE
    assert headers[aef_tracing.AGENT_HEADER_NAME] == "CI0000123"


def test_nothing_active_omits_the_headers_rather_than_sending_an_empty_one(fake_client, agent_id) -> None:
    """An absent header is what the mesh records as "nobody said"; an empty one passes validation
    and means nothing."""
    agent_id("")
    client = WorkstationClient(_config(), client_factory=fake_client[0])
    client._get_client()
    inner = fake_client[1][-1]
    inner.responses = [FakeResponse(200, {"id": "sess-1", "status": "ready", "quotas": {}})]

    assert aef_tracing.current_trace_id() == ""
    client.get_session("sess-1", "tok")

    headers = inner.calls[0]["headers"]
    assert aef_tracing.TRACE_HEADER_NAME not in headers
    assert aef_tracing.AGENT_HEADER_NAME not in headers
