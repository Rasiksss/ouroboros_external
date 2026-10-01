from __future__ import annotations

import base64
import json

import httpx
import pytest

from new_core.tools.schema import ToolSchema
from ouroboros.llm.providers.base import ProviderRequestError, ProviderResponseError
from ouroboros.llm.providers.gigachat_b2bank import (
    GigachatB2BankProvider,
    _api_url,
    _bounded_error_body,
    _decode_data_image_url,
    _prepared_images,
    _PreparedImage,
    _upload_b2bank_image,
    build_payload,
    build_tool_name_map,
    normalize_response,
    provider_function_name,
    runtime_function_name,
)


def test_b2bank_function_alias_is_reversible_and_letter_only():
    runtime_name = "ext_15_r_as_stroyka_pm_get"
    names = build_tool_name_map(
        [
            {
                "type": "function",
                "function": {"name": runtime_name, "parameters": {}},
            }
        ],
        [],
    )

    alias = names.runtime_to_provider[runtime_name]

    assert alias != runtime_name
    assert alias.startswith("f")
    assert len(alias) == 11
    assert alias.isalpha()
    assert not any(ch.isdigit() for ch in alias)
    assert runtime_function_name(alias, names) == runtime_name
    assert provider_function_name(runtime_name) == alias
    assert provider_function_name("lookup_weather") == "lookup_weather"
    assert runtime_function_name("lookup_weather") == "lookup_weather"


def test_build_payload_router_forwards_reasoning_effort_to_both_versions():
    # The dispatcher must thread the effort into v1 (payload root) and v2
    # (model_options.reasoning); dropping either forwarding line must fail here.
    v1 = build_payload(
        target={"resolved_model": "GigaChat-2-Max", "chat_api_version": "v1"},
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        tool_choice="auto",
        temperature=None,
        tools=None,
        reasoning_effort="low",
    )
    assert v1["reasoning_effort"] == "low"

    v2 = build_payload(
        target={"resolved_model": "GigaChat-2-Max", "chat_api_version": "v2"},
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        tool_choice="none",
        temperature=None,
        tools=None,
        reasoning_effort="low",
    )
    assert v2["model_options"]["reasoning"] == {"effort": "low"}


def test_b2bank_payload_aliases_extension_tool_names():
    runtime_name = "ext_15_r_as_stroyka_pm_get"
    alias = provider_function_name(runtime_name)

    payload = build_payload(
        target={"resolved_model": "GigaChat-2-Max"},
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=512,
        tool_choice={"type": "function", "function": {"name": runtime_name}},
        temperature=None,
        tools=[
            ToolSchema(
                name=runtime_name,
                description="Read AS Stroyka data",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            )
        ],
    )

    assert payload["functions"][0]["name"] == alias
    assert payload["function_call"] == {"name": alias}


def test_b2bank_payload_adds_properties_to_nested_object_parameters():
    payload = build_payload(
        target={"resolved_model": "GigaChat-2-Max"},
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=512,
        tool_choice="auto",
        temperature=None,
        tools=[
            ToolSchema(
                name="ext_15_r_as_stroyka_pm_get",
                description="Read AS Stroyka data",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "params": {
                            "type": "object",
                            "description": "Optional flat query-string object.",
                        },
                    },
                    "required": ["path"],
                },
            )
        ],
    )

    assert payload["functions"][0]["parameters"]["properties"]["params"] == {
        "type": "object",
        "description": "Optional flat query-string object.",
        "properties": {},
    }


def test_b2bank_payload_aliases_extension_tool_history():
    runtime_name = "ext_15_r_as_stroyka_pm_get"
    alias = provider_function_name(runtime_name)

    payload = build_payload(
        target={"resolved_model": "GigaChat-2-Max"},
        messages=[
            {"role": "user", "content": "Find project"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": runtime_name,
                            "arguments": '{"path":"/api/user/sub-projects"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": '{"status": 200}',
            },
        ],
        max_tokens=512,
        tool_choice="auto",
        temperature=None,
        tools=[],
    )

    assert payload["messages"][1]["function_call"] == {
        "name": alias,
        "arguments": {"path": "/api/user/sub-projects"},
    }
    assert payload["messages"][2] == {
        "role": "function",
        "name": alias,
        "content": '{"status": 200}',
    }


def test_b2bank_response_maps_alias_back_to_runtime_tool_name():
    runtime_name = "ext_15_r_as_stroyka_pm_get"
    names = build_tool_name_map(
        [
            {
                "type": "function",
                "function": {"name": runtime_name, "parameters": {}},
            }
        ],
        [],
    )
    alias = names.runtime_to_provider[runtime_name]

    message, _usage = normalize_response(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "function_call": {
                            "name": alias,
                            "arguments": {"path": "/api/user/sub-projects"},
                        },
                    },
                    "finish_reason": "function_call",
                }
            ],
            "usage": {},
        },
        {"resolved_model": "GigaChat-2-Max"},
        tool_names=names,
        request_id="request-1",
    )

    assert message["tool_calls"][0]["function"]["name"] == runtime_name
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {
        "path": "/api/user/sub-projects",
    }


def test_b2bank_api_url_preserves_configured_version():
    assert _api_url("https://giga.local", "/files") == "https://giga.local/v1/files"
    assert _api_url("https://giga.local/v1", "/files") == "https://giga.local/v1/files"
    assert _api_url("https://giga.local/v2", "/files") == "https://giga.local/v1/files"


def test_b2bank_image_data_url_without_mime_keeps_png_compatibility():
    encoded = base64.b64encode(b"png-data").decode()

    raw, mime = _decode_data_image_url(f"data:;base64,{encoded}")

    assert raw == b"png-data"
    assert mime == "image/png"


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://b2bank.test")
            response = httpx.Response(
                self.status_code,
                request=request,
                json=self._payload,
            )
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=request,
                response=response,
            )

    def json(self):
        return self._payload

    @property
    def headers(self):
        return {}

    @property
    def text(self):
        return json.dumps(self._payload)


class _FakeClient:
    def __init__(self, post):
        self._post = post
        self.closed = False

    def post(self, url, **kwargs):
        return self._post(url, **kwargs)

    def close(self):
        self.closed = True


def _install_fake_client(monkeypatch, post):
    client = _FakeClient(post)
    monkeypatch.setattr(
        GigachatB2BankProvider,
        "_get_client",
        lambda self, target, *, no_proxy: client,
    )
    return client


def _target():
    return {
        "resolved_model": "GigaChat-2-Max",
        "base_url": "https://b2bank.test/v1",
        "verify_ssl": False,
        "client_id": "client-1",
        "chat_api_version": "v1",
    }


def test_b2bank_chat_propagates_current_trace_id(monkeypatch):
    from ouroboros.observability import aef_tracing

    seen_headers = {}

    def fake_post(_url, **kwargs):
        seen_headers.update(kwargs["headers"])
        return _FakeResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            }
        )

    _install_fake_client(monkeypatch, fake_post)
    trace_id = "463ac35c9f6413ad48485a3953bb6124"
    with aef_tracing.trace_scope(trace_id):
        GigachatB2BankProvider().chat(
            _target(),
            [{"role": "user", "content": "hello"}],
            tools=None,
            reasoning_effort="medium",
            max_tokens=32,
            tool_choice="auto",
            temperature=None,
            no_proxy=False,
        )

    assert seen_headers["X-Trace-Id"] == trace_id


def test_b2bank_uploads_base64_image_and_sends_attachment(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/files"):
            return _FakeResponse({"id": "file-image-1"})
        return _FakeResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": "seen"}}],
                "usage": {},
            }
        )

    _install_fake_client(monkeypatch, fake_post)
    image_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode()
    message, _usage = GigachatB2BankProvider().chat(
        _target(),
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                ],
            }
        ],
        tools=None,
        reasoning_effort="none",
        max_tokens=512,
        tool_choice="auto",
        temperature=None,
        no_proxy=False,
    )

    assert message["content"] == "seen"
    assert len(calls) == 2
    upload_url, upload_kwargs = calls[0]
    assert upload_url == "https://b2bank.test/v1/files"
    assert upload_kwargs["data"] == {"purpose": "general"}
    assert upload_kwargs["files"]["file"][0].endswith(".png")
    assert upload_kwargs["files"]["file"][1] == b"\x89PNG\r\n\x1a\nimage"
    assert upload_kwargs["files"]["file"][2] == "image/png"
    assert "Content-Type" not in upload_kwargs["headers"]

    chat_payload = calls[1][1]["json"]
    assert chat_payload["messages"] == [
        {
            "role": "user",
            "content": "Describe",
            "attachments": ["file-image-1"],
        }
    ]


def test_b2bank_splits_multiple_images_into_one_attachment_per_message(monkeypatch):
    calls = []
    upload_ids = iter(["file-image-1", "file-image-2"])

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/files"):
            return _FakeResponse({"id": next(upload_ids)})
        return _FakeResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": "seen"}}],
                "usage": {},
            }
        )

    _install_fake_client(monkeypatch, fake_post)
    first = base64.b64encode(b"first").decode()
    second = base64.b64encode(b"second").decode()

    GigachatB2BankProvider().chat(
        _target(),
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{first}"}},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{second}"}},
                ],
            }
        ],
        tools=None,
        reasoning_effort="none",
        max_tokens=512,
        tool_choice="auto",
        temperature=None,
        no_proxy=False,
    )

    chat_payload = calls[-1][1]["json"]
    assert chat_payload["messages"] == [
        {"role": "user", "content": "Describe", "attachments": ["file-image-1"]},
        {"role": "user", "content": "Describe", "attachments": ["file-image-2"]},
    ]


def test_b2bank_rejects_remote_image_url_before_chat_request(monkeypatch):
    calls = []
    _install_fake_client(monkeypatch, lambda url, **kwargs: calls.append((url, kwargs)))

    with pytest.raises(ProviderRequestError, match="image_base64"):
        GigachatB2BankProvider().chat(
            _target(),
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe"},
                        {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}},
                    ],
                }
            ],
            tools=None,
            reasoning_effort="none",
            max_tokens=512,
            tool_choice="auto",
            temperature=None,
            no_proxy=False,
        )

    assert calls == []


def test_b2bank_rejects_image_over_decoded_size_limit(monkeypatch):
    calls = []
    _install_fake_client(monkeypatch, lambda url, **kwargs: calls.append((url, kwargs)))
    image_b64 = base64.b64encode(b"x" * 15_000_001).decode()

    with pytest.raises(ProviderRequestError, match="image.*too large"):
        GigachatB2BankProvider().chat(
            _target(),
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    ],
                }
            ],
            tools=None,
            reasoning_effort="none",
            max_tokens=512,
            tool_choice="auto",
            temperature=None,
            no_proxy=False,
        )

    assert calls == []


def test_b2bank_image_upload_failure_reports_clear_error(monkeypatch):
    def fake_post(url, **kwargs):
        if url.endswith("/files"):
            return _FakeResponse({"message": "bad file"}, status_code=500)
        return _FakeResponse({"unexpected": True})

    _install_fake_client(monkeypatch, fake_post)
    image_b64 = base64.b64encode(b"image").decode()

    with pytest.raises(httpx.HTTPStatusError, match="B2Bank image upload HTTP status 500"):
        GigachatB2BankProvider().chat(
            _target(),
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    ],
                }
            ],
            tools=None,
            reasoning_effort="none",
            max_tokens=512,
            tool_choice="auto",
            temperature=None,
            no_proxy=False,
        )


def test_b2bank_malformed_image_upload_is_provider_response_error():
    client = type("Client", (), {"post": staticmethod(lambda *_args, **_kwargs: _FakeResponse({}))})()

    with pytest.raises(ProviderResponseError):
        _upload_b2bank_image(
            client=client,
            target=_target(),
            headers={},
            image=_PreparedImage(b"image", "image/png", "digest"),
            timeout=None,
            request_id="request-id",
        )


def test_b2bank_error_body_exposes_metadata_but_not_payload():
    response = _FakeResponse(
        {
            "status": 422,
            "code": "invalid_request",
            "message": "validation failed for secret user prompt",
            "messages": [{"content": "secret user prompt"}],
        }
    )

    summary = _bounded_error_body(response)

    assert "422" in summary
    assert "invalid_request" in summary
    assert "message" in summary
    assert "secret user prompt" not in summary


def test_b2bank_prevalidates_all_images_before_first_upload(monkeypatch):
    calls = []
    _install_fake_client(monkeypatch, lambda url, **kwargs: calls.append((url, kwargs)))
    valid = base64.b64encode(b"valid").decode()

    with pytest.raises(ProviderRequestError, match="empty"):
        GigachatB2BankProvider().chat(
            _target(),
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{valid}"}},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,"}},
                    ],
                }
            ],
            tools=None,
            reasoning_effort="none",
            max_tokens=32,
            tool_choice="auto",
            temperature=None,
            no_proxy=False,
        )

    assert calls == []


def test_b2bank_rejects_image_count_and_total_limits_before_upload(monkeypatch):
    calls = []
    _install_fake_client(monkeypatch, lambda url, **kwargs: calls.append((url, kwargs)))
    encoded = base64.b64encode(b"x").decode()
    content = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}} for _ in range(11)]

    with pytest.raises(ProviderRequestError, match="too many images"):
        GigachatB2BankProvider().chat(
            _target(),
            [{"role": "user", "content": content}],
            tools=None,
            reasoning_effort="none",
            max_tokens=32,
            tool_choice="auto",
            temperature=None,
            no_proxy=False,
        )

    monkeypatch.setattr(
        "ouroboros.llm.providers.gigachat_b2bank._MAX_B2BANK_TOTAL_IMAGE_BYTES",
        5,
    )
    three_bytes = base64.b64encode(b"abc").decode()
    with pytest.raises(ProviderRequestError, match="too large in total"):
        GigachatB2BankProvider().chat(
            _target(),
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{three_bytes}"}},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{three_bytes}"}},
                    ],
                }
            ],
            tools=None,
            reasoning_effort="none",
            max_tokens=32,
            tool_choice="auto",
            temperature=None,
            no_proxy=False,
        )

    assert calls == []


def test_b2bank_checks_count_before_decoding_images(monkeypatch):
    decode_calls = 0

    def fake_decode(_url):
        nonlocal decode_calls
        decode_calls += 1
        return b"x", "image/png"

    monkeypatch.setattr(
        "ouroboros.llm.providers.gigachat_b2bank._decode_data_image_url",
        fake_decode,
    )
    messages = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "unused"}} for _ in range(11)],
        }
    ]

    with pytest.raises(ValueError, match="too many images"):
        _prepared_images(messages)

    assert decode_calls == 0


def test_b2bank_stops_decoding_when_total_limit_is_exceeded(monkeypatch):
    decode_calls = 0

    def fake_decode(_url):
        nonlocal decode_calls
        decode_calls += 1
        return b"abc", "image/png"

    monkeypatch.setattr(
        "ouroboros.llm.providers.gigachat_b2bank._decode_data_image_url",
        fake_decode,
    )
    monkeypatch.setattr(
        "ouroboros.llm.providers.gigachat_b2bank._MAX_B2BANK_TOTAL_IMAGE_BYTES",
        5,
    )
    messages = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "unused"}} for _ in range(3)],
        }
    ]

    with pytest.raises(ValueError, match="too large in total"):
        _prepared_images(messages)

    assert decode_calls == 2


def test_b2bank_accepts_string_form_image_url_from_production_flow(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/files"):
            return _FakeResponse({"id": "string-image"})
        return _FakeResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            }
        )

    _install_fake_client(monkeypatch, fake_post)
    encoded = base64.b64encode(b"string-form").decode()
    GigachatB2BankProvider().chat(
        _target(),
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": f"data:image/png;base64,{encoded}",
                    }
                ],
            }
        ],
        tools=None,
        reasoning_effort="none",
        max_tokens=32,
        tool_choice="auto",
        temperature=None,
        no_proxy=False,
    )

    assert calls[0][0] == "https://b2bank.test/v1/files"
    assert calls[-1][1]["json"]["messages"][0]["attachments"] == ["string-image"]


def test_b2bank_deduplicates_uploads_and_preserves_existing_attachments(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/files"):
            return _FakeResponse({"id": "uploaded-1"})
        return _FakeResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            }
        )

    _install_fake_client(monkeypatch, fake_post)
    encoded = base64.b64encode(b"same-image").decode()
    GigachatB2BankProvider().chat(
        _target(),
        [
            {
                "role": "user",
                "attachments": ["existing-1"],
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                ],
            }
        ],
        tools=None,
        reasoning_effort="none",
        max_tokens=32,
        tool_choice="auto",
        temperature=None,
        no_proxy=False,
    )

    assert [url for url, _ in calls].count("https://b2bank.test/v1/files") == 1
    assert calls[-1][1]["json"]["messages"] == [
        {"role": "user", "content": "look", "attachments": ["existing-1", "uploaded-1"]},
        {"role": "user", "content": "look", "attachments": ["existing-1", "uploaded-1"]},
    ]


def test_b2bank_v2_uses_v1_upload_and_files_content_blocks(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/files"):
            return _FakeResponse({"id": "uploaded-v2"})
        return _FakeResponse(
            {
                "messages": [{"role": "assistant", "content": [{"text": "ok"}]}],
                "finish_reason": "stop",
                "usage": {},
            }
        )

    _install_fake_client(monkeypatch, fake_post)
    encoded = base64.b64encode(b"TIFF-data").decode()
    target = {**_target(), "chat_api_version": "v2"}
    GigachatB2BankProvider().chat(
        target,
        [
            {
                "role": "user",
                "attachments": ["existing-v2"],
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": f"data:image/tiff;base64,{encoded}"}},
                ],
            }
        ],
        tools=None,
        reasoning_effort="none",
        max_tokens=32,
        tool_choice="auto",
        temperature=None,
        no_proxy=False,
    )

    assert calls[0][0] == "https://b2bank.test/v1/files"
    assert calls[0][1]["files"]["file"][0].endswith(".tiff")
    assert calls[1][0] == "https://b2bank.test/v2/chat/completions"
    assert calls[1][1]["json"]["messages"] == [
        {
            "role": "user",
            "content": [
                {"text": "look"},
                {"files": [{"id": "existing-v2"}, {"id": "uploaded-v2"}]},
            ],
        }
    ]


def test_b2bank_bmp_upload_uses_compatible_extension(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/files"):
            return _FakeResponse({"id": "bmp-1"})
        return _FakeResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            }
        )

    _install_fake_client(monkeypatch, fake_post)
    encoded = base64.b64encode(b"BMP-data").decode()
    GigachatB2BankProvider().chat(
        _target(),
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/bmp;base64,{encoded}"},
                    }
                ],
            }
        ],
        tools=None,
        reasoning_effort="none",
        max_tokens=32,
        tool_choice="auto",
        temperature=None,
        no_proxy=False,
    )

    assert calls[0][1]["files"]["file"][0].endswith(".bmp")


def test_b2bank_httpx_client_reuse_tls_no_proxy_and_close(monkeypatch):
    created = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.close_calls = 0
            created.append(self)

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr("ouroboros.llm.providers.gigachat_b2bank.httpx.Client", FakeClient)
    provider = GigachatB2BankProvider()
    target = {
        **_target(),
        "client_cert_path": "/tmp/client.crt",
        "client_key_path": "/tmp/client.key",
    }

    first = provider._get_client(target, no_proxy=False)
    assert provider._get_client(target, no_proxy=False) is first
    no_proxy_client = provider._get_client(target, no_proxy=True)

    assert len(created) == 2
    assert first.kwargs["verify"] is False
    assert first.kwargs["cert"] == ("/tmp/client.crt", "/tmp/client.key")
    assert first.kwargs["trust_env"] is True
    assert no_proxy_client.kwargs["trust_env"] is False
    assert "transport" not in first.kwargs

    provider.close()
    provider.close()
    assert [client.close_calls for client in created] == [1, 1]


@pytest.mark.parametrize(
    ("read_timeout_sec", "phase_cap_sec", "expected"),
    [
        (None, 8, (8, 8, 8, 8)),
        (120, None, (11, 120, 90, 11)),
        (120, 7, (7, 7, 7, 7)),
    ],
)
def test_b2bank_http_timeout_separates_read_override_from_phase_cap(
    monkeypatch, read_timeout_sec, phase_cap_sec, expected
):
    from ouroboros.storage.settings.schema import AppSettings

    calls = []
    monkeypatch.setattr(
        "ouroboros.storage.settings.store.load_settings",
        lambda: AppSettings(
            OUROBOROS_LLM_HTTP_CONNECT_TIMEOUT_SEC=11,
            OUROBOROS_LLM_HTTP_READ_TIMEOUT_SEC=90,
        ),
    )

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            }
        )

    _install_fake_client(monkeypatch, fake_post)
    GigachatB2BankProvider().chat(
        _target(),
        [{"role": "user", "content": "hello"}],
        tools=None,
        reasoning_effort="none",
        max_tokens=32,
        tool_choice="auto",
        temperature=None,
        no_proxy=False,
        read_timeout_sec=read_timeout_sec,
        phase_cap_sec=phase_cap_sec,
    )

    timeout = calls[0][1]["timeout"]
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == expected


_HISTORY_CALL = {"id": "c1", "type": "function", "function": {"name": "noop", "arguments": "{}"}}


@pytest.mark.parametrize(
    ("messages", "target_overrides"),
    [
        # Multiple assistant tool calls don't fit the v1 wire format.
        (
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "", "tool_calls": [_HISTORY_CALL, {**_HISTORY_CALL, "id": "c2"}]},
            ],
            {},
        ),
        # Image validation ValueError (empty base64 payload).
        ([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,"}}]}], {}),
        # Misconfigured chat API version raises ValueError before any request.
        ([{"role": "user", "content": "hello"}], {"chat_api_version": "v3"}),
    ],
    ids=["multiple-tool-calls", "invalid-image", "invalid-api-version"],
)
def test_b2bank_unrepresentable_request_raises_request_error_before_http(monkeypatch, messages, target_overrides):
    """Deterministic request-side validation is not a provider fault: no POST,
    no breaker blame, while the fallback provider may still accept the request."""
    posts = []
    _install_fake_client(monkeypatch, lambda url, **kwargs: posts.append(url))

    with pytest.raises(ProviderRequestError):
        GigachatB2BankProvider().chat(
            {**_target(), **target_overrides},
            messages,
            tools=None,
            reasoning_effort="none",
            max_tokens=32,
            tool_choice="auto",
            temperature=None,
            no_proxy=False,
        )

    assert posts == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "choices": [{"message": {"role": "assistant", "content": []}}],
            "usage": {},
        },
        {
            "choices": [{"message": {"role": "assistant", "content": "ok", "function_call": []}}],
            "usage": {},
        },
    ],
)
def test_b2bank_malformed_response_raises_provider_response_error(monkeypatch, payload):
    # ProviderResponseError classifies as INCOMPLETE, so background routing tries
    # the fallback — that path is covered in test_provider_failure_reporting.
    _install_fake_client(monkeypatch, lambda *_args, **_kwargs: _FakeResponse(payload))

    with pytest.raises(ProviderResponseError):
        GigachatB2BankProvider().chat(
            _target(),
            [{"role": "user", "content": "hello"}],
            tools=None,
            reasoning_effort="none",
            max_tokens=32,
            tool_choice="auto",
            temperature=None,
            no_proxy=False,
        )


# Distinct _normalize_usage branches (missing, non-dict, malformed fields) once
# each, spread across the two payload shapes instead of a full 2x3 product.
@pytest.mark.parametrize(
    ("version", "raw_usage", "expected_counts"),
    [
        ("v2", None, (0, 0, 0, 0)),
        ("v1", [], (0, 0, 0, 0)),
        (
            "v1",
            {
                "prompt_tokens": "12.0",
                "completion_tokens": 7,
                "total_tokens": {"invalid": True},
                "prompt_tokens_details": [],
            },
            (0, 7, 0, 0),
        ),
    ],
)
def test_b2bank_preserves_assistant_response_when_usage_is_missing_or_malformed(version, raw_usage, expected_counts):
    target = {"chat_api_version": version, "resolved_model": "GigaChat-Pro", "usage_model": "billing-model"}
    if version == "v1":
        payload = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "the answer is 42"},
                    "finish_reason": "stop",
                }
            ]
        }
    else:
        payload = {
            "finish_reason": "stop",
            "messages": [{"role": "assistant", "content": [{"text": "the answer is 42"}]}],
        }
    if raw_usage is not None:
        payload["usage"] = raw_usage

    message, usage = normalize_response(payload, target)

    assert message == {"role": "assistant", "content": "the answer is 42", "finish_reason": "stop"}
    assert (
        usage["prompt_tokens"],
        usage["completion_tokens"],
        usage["total_tokens"],
        usage["cached_tokens"],
    ) == expected_counts
    assert usage["provider"] == "gigachat-b2bank"
    assert usage["resolved_model"] == "billing-model"
