from __future__ import annotations

import pathlib
import ssl
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from new_core.tools.context import ToolContext
from ouroboros.observability import aef_tracing
from ouroboros.tools.image_generate import _image_generate_handler
from tests.helpers.files import outbound_bytes
from tests.helpers.http_double import FakeHttpResponse, RecordingHttpClient
from tests.helpers.outbound import RecordingOutbound
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, make_tool_ctx, make_tool_env
from tests.helpers.tls_listener import issue_client_certificate

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
TOOL_ERROR = "⚠️ TOOL_ERROR (image_generate):"
API = "https://fusion-brain.example/api/fusionbrain/api/v1"
PIPELINE_ID = "pipeline-under-test"


@pytest.fixture()
def http(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> RecordingHttpClient:
    http = RecordingHttpClient()

    def _open(**kwargs: Any) -> RecordingHttpClient:
        http.kwargs = kwargs
        return http

    tls = tmp_path / "tls"
    tls.mkdir()
    ca_path, certificate_path, key_path = issue_client_certificate(tls, common_name="ouroboros-dev")
    monkeypatch.setenv("FUSIONBRAIN_BASE_URL", "https://fusion-brain.example/")
    monkeypatch.setenv("FUSIONBRAIN_PIPELINE_ID", PIPELINE_ID)
    monkeypatch.setenv("FUSIONBRAIN_CERT_PATH", str(certificate_path))
    monkeypatch.setenv("FUSIONBRAIN_KEY_PATH", str(key_path))
    monkeypatch.setenv("FUSIONBRAIN_CA_PATH", str(ca_path))
    monkeypatch.setattr(httpx, "Client", _open)
    return http


def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path, task_workspace=tmp_path, outbound=RecordingOutbound())


def _drawn(http: RecordingHttpClient) -> None:
    http.responses.extend(
        [
            FakeHttpResponse({"request_id": "req-1"}),
            FakeHttpResponse({"status": "DONE", "result": {"censored": False, "result_image": "file-1"}}),
            FakeHttpResponse(content=PNG),
        ]
    )


def test_success_runs_the_three_steps_and_delivers_the_image(http: RecordingHttpClient, tmp_path: pathlib.Path) -> None:
    _drawn(http)
    ctx = _ctx(tmp_path)

    result = _image_generate_handler(ctx, query="  розовый кот  ")

    assert result.startswith("OK:")
    assert [call["url"] for call in http.calls] == [
        f"{API}/pipeline/execute",
        f"{API}/pipeline/status",
        f"{API}/storage/download/file-1",
    ]
    assert http.calls[0]["json"] == {"pipeline_id": PIPELINE_ID, "pipeline_data": {"query": "розовый кот"}}
    assert http.calls[1]["json"] == {"request_id": "req-1"}
    image, kwargs = ctx.outbound.images[0]
    assert outbound_bytes(image, ctx.scope) == PNG
    assert kwargs["mime"] == "image/png"


def test_the_status_is_polled_until_the_file_is_ready(
    http: RecordingHttpClient, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(
        "ouroboros.tools.image_generate.time",
        SimpleNamespace(monotonic=lambda: 0.0, sleep=slept.append),
    )
    http.responses.extend(
        [
            FakeHttpResponse({"request_id": "req-1"}),
            FakeHttpResponse({"status": "PROCESSING", "result": {}}),
            FakeHttpResponse({"status": "DONE", "result": {"censored": False, "result_image": "file-1"}}),
            FakeHttpResponse(content=PNG),
        ]
    )

    result = _image_generate_handler(_ctx(tmp_path), query="кот")

    assert result.startswith("OK:")
    assert len(slept) == 1
    assert [call["url"] for call in http.calls[1:3]] == [f"{API}/pipeline/status", f"{API}/pipeline/status"]


def test_waiting_past_the_deadline_stops(
    http: RecordingHttpClient, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter([0.0, 1000.0])
    monkeypatch.setattr(
        "ouroboros.tools.image_generate.time",
        SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None),
    )
    http.responses.append(FakeHttpResponse({"request_id": "req-1"}))
    http.responses.append(FakeHttpResponse({"status": "PROCESSING", "result": {}}))
    ctx = _ctx(tmp_path)

    result = _image_generate_handler(ctx, query="кот")

    assert result.startswith(TOOL_ERROR)
    assert "was not ready" in result
    assert ctx.outbound.images == []


def test_an_image_that_arrives_after_the_tool_budget_is_not_delivered(
    http: RecordingHttpClient, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, _ = make_tool_env(tmp_path / "repo", tmp_path / "data")
    budget = catalog.find("image_generate", TEST_SCOPE).timeout_sec
    clock = iter([0.0, float(budget)])
    monkeypatch.setattr("ouroboros.tools.image_generate.time", SimpleNamespace(monotonic=lambda: next(clock)))
    _drawn(http)
    ctx = _ctx(tmp_path)

    result = _image_generate_handler(ctx, query="кот")

    assert result.startswith(TOOL_ERROR)
    assert ctx.outbound.images == []


def test_a_failed_generation_is_a_tool_failure(http: RecordingHttpClient, tmp_path: pathlib.Path) -> None:
    http.responses.append(FakeHttpResponse({"request_id": "req-1"}))
    http.responses.append(FakeHttpResponse({"status": "ERROR", "result": {}}))
    ctx = _ctx(tmp_path)

    result = _image_generate_handler(ctx, query="кот")

    assert result.startswith(TOOL_ERROR)
    assert ctx.outbound.images == []


@parametrize_from_dict(
    {
        "censored": {
            "result": {"censored": True},
            "message": "censor",
        },
        "done without a file": {
            "result": {"censored": False},
            "message": "carries no file",
        },
    }
)
def test_a_finished_generation_without_a_picture_says_why(
    http: RecordingHttpClient, tmp_path: pathlib.Path, result: dict[str, Any], message: str
) -> None:
    http.responses.append(FakeHttpResponse({"request_id": "req-1"}))
    http.responses.append(FakeHttpResponse({"status": "DONE", "result": result}))

    answer = _image_generate_handler(_ctx(tmp_path), query="кот")

    assert answer.startswith(TOOL_ERROR)
    assert message in answer


def test_a_busy_pipeline_asks_to_try_again(http: RecordingHttpClient, tmp_path: pathlib.Path) -> None:
    http.responses.append(FakeHttpResponse({"message": "BUSY"}, status_code=429))

    result = _image_generate_handler(_ctx(tmp_path), query="кот")

    assert result.startswith(TOOL_ERROR)
    assert "busy" in result


@parametrize_from_dict(
    {
        "a style reaches the pipeline": {"style": "picasso", "expected": {"query": "кот", "style": "picasso"}},
        "no style is not sent": {"style": None, "expected": {"query": "кот"}},
    }
)
def test_the_style_is_optional(
    http: RecordingHttpClient, tmp_path: pathlib.Path, style: str | None, expected: dict[str, Any]
) -> None:
    _drawn(http)

    _image_generate_handler(_ctx(tmp_path), query="кот", style=style)

    assert http.calls[0]["json"]["pipeline_data"] == expected


@parametrize_from_dict(
    {
        "bare host": {"configured": "https://fusion-brain.example"},
        "trailing slash": {"configured": "https://fusion-brain.example/"},
        "the address as the service documents it": {"configured": f"{API}/"},
    }
)
def test_the_api_path_is_not_doubled(
    http: RecordingHttpClient, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    monkeypatch.setenv("FUSIONBRAIN_BASE_URL", configured)
    _drawn(http)

    _image_generate_handler(_ctx(tmp_path), query="кот")

    assert http.calls[0]["url"] == f"{API}/pipeline/execute"


@parametrize_from_dict(
    {
        "blank query": {"arguments": {"query": "   "}},
        "null query": {"arguments": {"query": None}},
    }
)
def test_invalid_arguments_never_reach_the_api(
    http: RecordingHttpClient, tmp_path: pathlib.Path, arguments: dict[str, Any]
) -> None:
    result = _image_generate_handler(_ctx(tmp_path), **arguments)

    assert result.startswith(TOOL_ERROR)
    assert http.calls == []


@parametrize_from_dict(
    {
        "no base url": {"unset": "FUSIONBRAIN_BASE_URL", "message": "FUSIONBRAIN_BASE_URL is not set"},
        "no pipeline": {"unset": "FUSIONBRAIN_PIPELINE_ID", "message": "FUSIONBRAIN_PIPELINE_ID is not set"},
        "certificate without its key": {"unset": "FUSIONBRAIN_KEY_PATH", "message": "must be set together"},
    }
)
def test_incomplete_configuration_never_reaches_the_api(
    http: RecordingHttpClient, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, unset: str, message: str
) -> None:
    monkeypatch.setenv(unset, "")

    result = _image_generate_handler(_ctx(tmp_path), query="кот")

    assert result.startswith(TOOL_ERROR)
    assert message in result
    assert http.calls == []


@parametrize_from_dict(
    {
        "verification on": {"verify_ssl": "true", "expected_mode": ssl.CERT_REQUIRED},
        "verification off": {"verify_ssl": "false", "expected_mode": ssl.CERT_NONE},
    }
)
def test_the_connection_carries_the_client_certificate_together_with_the_ca(
    http: RecordingHttpClient,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    verify_ssl: str,
    expected_mode: ssl.VerifyMode,
) -> None:
    monkeypatch.setenv("FUSIONBRAIN_VERIFY_SSL", verify_ssl)
    _drawn(http)

    _image_generate_handler(_ctx(tmp_path), query="кот")

    context = http.kwargs["verify"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == expected_mode
    authorities = [name for trusted in context.get_ca_certs() for rdn in trusted["subject"] for name in rdn]
    assert ("commonName", "ouroboros-test-ca") in authorities
    assert "cert" not in http.kwargs


def test_a_deployment_without_a_client_certificate_still_draws(
    http: RecordingHttpClient, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSIONBRAIN_CERT_PATH", "")
    monkeypatch.setenv("FUSIONBRAIN_KEY_PATH", "")
    _drawn(http)

    result = _image_generate_handler(_ctx(tmp_path), query="кот")

    assert result.startswith("OK:")
    assert "cert" not in http.kwargs


def test_http_error_is_a_bounded_tool_failure(http: RecordingHttpClient, tmp_path: pathlib.Path) -> None:
    http.responses.append(FakeHttpResponse(status_code=400, text="<html>" + "x" * 5000 + "</html>"))

    result = _image_generate_handler(_ctx(tmp_path), query="кот")

    assert result.startswith(TOOL_ERROR)
    assert "HTTP 400" in result
    assert len(result) < 2_500


def test_a_download_that_carries_no_image_says_so(http: RecordingHttpClient, tmp_path: pathlib.Path) -> None:
    http.responses.append(FakeHttpResponse({"request_id": "req-1"}))
    http.responses.append(FakeHttpResponse({"status": "DONE", "result": {"censored": False, "result_image": "file-1"}}))
    http.responses.append(FakeHttpResponse({"status": 402, "message": "no access"}))

    result = _image_generate_handler(_ctx(tmp_path), query="кот")

    assert result.startswith(TOOL_ERROR)
    assert "carries no image" in result
    assert "402" in result


def test_transport_failure_is_a_tool_failure(http: RecordingHttpClient, tmp_path: pathlib.Path) -> None:
    http.responses.append(httpx.ConnectError("connection refused"))

    result = _image_generate_handler(_ctx(tmp_path), query="кот")

    assert result.startswith(TOOL_ERROR)


def test_request_carries_the_operation_identifier(http: RecordingHttpClient, tmp_path: pathlib.Path) -> None:
    trace_id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    _drawn(http)

    with aef_tracing.trace_scope(trace_id):
        _image_generate_handler(_ctx(tmp_path), query="кот")

    assert http.kwargs["headers"]["X-Trace-Id"] == trace_id
