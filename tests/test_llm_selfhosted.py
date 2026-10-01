from __future__ import annotations

from types import SimpleNamespace

import pytest

from ouroboros.app.server_runtime import apply_runtime_provider_defaults, has_remote_provider
from ouroboros.llm.client import LLMClient
from ouroboros.llm.models import parse_model_ref
from ouroboros.llm.provider_models import normalize_openai_compatible_base_url
from ouroboros.llm.providers.openai_compat import (
    OpenAICompatibleProvider,
    build_remote_kwargs,
    make_no_proxy_client,
    strip_authorization_header,
)
from ouroboros.storage.settings.schema import AppSettings


class _FakeOpenAIResponse:
    def model_dump(self):
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }


def _capture_openai_http_client(monkeypatch):
    import httpx
    import openai

    seen: dict[str, object] = {}

    class _FakeHttpClient:
        def __init__(self, **kwargs):
            seen["httpx_kwargs"] = kwargs

        def close(self):
            seen["closed"] = True

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            seen["openai_kwargs"] = kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: _FakeOpenAIResponse()))

    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    return seen


def test_normalize_openai_compatible_base_url_appends_v1():
    assert normalize_openai_compatible_base_url("https://llm.local") == "https://llm.local/v1"
    assert normalize_openai_compatible_base_url("https://llm.local/") == "https://llm.local/v1"
    assert normalize_openai_compatible_base_url("https://llm.local/v1") == "https://llm.local/v1"
    assert normalize_openai_compatible_base_url("https://llm.local/v1/") == "https://llm.local/v1"
    assert normalize_openai_compatible_base_url("https://llm.local/v2") == "https://llm.local/v2"
    assert normalize_openai_compatible_base_url("https://llm.local/api/v2") == "https://llm.local/api/v2"


def test_resolve_selfhosted_target_appends_v1_to_base_url(monkeypatch):
    monkeypatch.setenv(
        "SELFHOSTED_LLM_BASE_URL",
        "https://example.modelrun.inference.cloud.ru",
    )
    monkeypatch.delenv("SELFHOSTED_LLM_API_KEY", raising=False)

    target = LLMClient()._resolve_remote_target("my-model")

    assert target["base_url"] == "https://example.modelrun.inference.cloud.ru/v1"
    assert target["api_key"] == ""


def test_resolve_selfhosted_target_uses_configured_base_url(monkeypatch):
    monkeypatch.setenv("SELFHOSTED_LLM_BASE_URL", "http://llm.local/v1")
    monkeypatch.setenv("SELFHOSTED_LLM_API_KEY", "local-key")

    target = LLMClient()._resolve_remote_target("my-llama-3")

    assert target["provider"] == "selfhosted"
    assert target["api_key"] == "local-key"
    assert target["base_url"] == "http://llm.local/v1"
    assert target["resolved_model"] == "my-llama-3"
    assert target["usage_model"] == "selfhosted/my-llama-3"


def test_resolve_selfhosted_target_includes_tls_config(monkeypatch):
    monkeypatch.setenv("SELFHOSTED_LLM_BASE_URL", "https://llm.local/v1")
    monkeypatch.setenv("SELFHOSTED_LLM_API_KEY", "local-key")
    monkeypatch.setenv("SELFHOSTED_LLM_VERIFY_SSL", "false")
    monkeypatch.setenv("SELFHOSTED_LLM_CA_PATH", "/tmp/ca.pem")
    monkeypatch.setenv("SELFHOSTED_LLM_CERT_PATH", "/tmp/client.pem")
    monkeypatch.setenv("SELFHOSTED_LLM_KEY_PATH", "/tmp/client.key")

    target = LLMClient()._resolve_remote_target("my-llama-3")

    assert target["provider"] == "selfhosted"
    assert target["verify_ssl"] is False
    assert target["ca_path"] == "/tmp/ca.pem"
    assert target["client_cert_path"] == "/tmp/client.pem"
    assert target["client_key_path"] == "/tmp/client.key"


def test_selfhosted_http_client_keeps_default_tls_kwargs(monkeypatch):
    seen = _capture_openai_http_client(monkeypatch)

    OpenAICompatibleProvider("selfhosted")._get_remote_client(
        {
            "provider": "selfhosted",
            "base_url": "https://llm.local/v1",
            "api_key": "none",
            "default_headers": {},
        }
    )

    httpx_kwargs = seen["httpx_kwargs"]
    assert "timeout" in httpx_kwargs
    assert "verify" not in httpx_kwargs
    assert "cert" not in httpx_kwargs


def test_selfhosted_http_client_can_disable_server_cert_verification(monkeypatch):
    seen = _capture_openai_http_client(monkeypatch)

    OpenAICompatibleProvider("selfhosted")._get_remote_client(
        {
            "provider": "selfhosted",
            "base_url": "https://llm.local/v1",
            "api_key": "none",
            "default_headers": {},
            "verify_ssl": False,
        }
    )

    assert seen["httpx_kwargs"]["verify"] is False


def test_selfhosted_verify_false_ignores_ca_path(monkeypatch):
    seen = _capture_openai_http_client(monkeypatch)

    OpenAICompatibleProvider("selfhosted")._get_remote_client(
        {
            "provider": "selfhosted",
            "base_url": "https://llm.local/v1",
            "api_key": "none",
            "default_headers": {},
            "verify_ssl": False,
            "ca_path": "/tmp/ca.pem",
        }
    )

    assert seen["httpx_kwargs"]["verify"] is False


def test_selfhosted_http_client_uses_ca_path_when_verify_ssl_true(monkeypatch):
    seen = _capture_openai_http_client(monkeypatch)

    OpenAICompatibleProvider("selfhosted")._get_remote_client(
        {
            "provider": "selfhosted",
            "base_url": "https://llm.local/v1",
            "api_key": "none",
            "default_headers": {},
            "verify_ssl": True,
            "ca_path": "/tmp/ca.pem",
        }
    )

    assert seen["httpx_kwargs"]["verify"] == "/tmp/ca.pem"


def test_selfhosted_http_client_uses_client_cert_pair(monkeypatch):
    seen = _capture_openai_http_client(monkeypatch)

    OpenAICompatibleProvider("selfhosted")._get_remote_client(
        {
            "provider": "selfhosted",
            "base_url": "https://llm.local/v1",
            "api_key": "none",
            "default_headers": {},
            "client_cert_path": "/tmp/client.pem",
            "client_key_path": "/tmp/client.key",
        }
    )

    assert seen["httpx_kwargs"]["cert"] == ("/tmp/client.pem", "/tmp/client.key")


@pytest.mark.parametrize(
    "target_update",
    [
        {"client_cert_path": "/tmp/client.pem"},
        {"client_key_path": "/tmp/client.key"},
    ],
)
def test_selfhosted_http_client_rejects_incomplete_client_cert_pair(
    monkeypatch,
    target_update,
):
    _capture_openai_http_client(monkeypatch)
    target = {
        "provider": "selfhosted",
        "base_url": "https://llm.local/v1",
        "api_key": "none",
        "default_headers": {},
        **target_update,
    }

    with pytest.raises(ValueError, match="SELFHOSTED_LLM_CERT_PATH and SELFHOSTED_LLM_KEY_PATH"):
        OpenAICompatibleProvider("selfhosted")._get_remote_client(target)


def test_selfhosted_no_proxy_client_uses_tls_kwargs(monkeypatch):
    seen = _capture_openai_http_client(monkeypatch)

    message, usage = OpenAICompatibleProvider("selfhosted").chat(
        {
            "provider": "selfhosted",
            "resolved_model": "my-llama-3",
            "usage_model": "selfhosted/my-llama-3",
            "base_url": "https://llm.local/v1",
            "api_key": "none",
            "default_headers": {},
            "verify_ssl": False,
            "client_cert_path": "/tmp/client.pem",
            "client_key_path": "/tmp/client.key",
        },
        [{"role": "user", "content": "hi"}],
        tools=None,
        reasoning_effort="none",
        max_tokens=64,
        tool_choice="auto",
        temperature=None,
        no_proxy=True,
    )

    assert seen["httpx_kwargs"]["verify"] is False
    assert seen["httpx_kwargs"]["cert"] == ("/tmp/client.pem", "/tmp/client.key")
    assert seen["closed"] is True
    assert message["content"] == "ok"
    assert usage["provider"] == "selfhosted"


def test_selfhosted_chat_uses_openai_compatible_payload(monkeypatch):
    monkeypatch.setenv("SELFHOSTED_LLM_BASE_URL", "http://llm.local/v1")
    monkeypatch.delenv("SELFHOSTED_LLM_API_KEY", raising=False)
    client = LLMClient()
    seen: dict[str, object] = {}

    class _FakeResponse:
        def model_dump(self):
            return {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }

    def create(**kwargs):
        seen["kwargs"] = kwargs
        return _FakeResponse()

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider = OpenAICompatibleProvider("selfhosted")
    monkeypatch.setattr(provider, "_get_remote_client", lambda target: fake_client)

    def _resolve(model: str):
        ref = parse_model_ref(model)
        return provider, provider.resolve_target(ref)

    monkeypatch.setattr(client._registry, "resolve", _resolve)

    message, usage = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="my-llama-3",
        max_tokens=64,
    )

    kwargs = seen["kwargs"]
    assert kwargs["model"] == "my-llama-3"
    assert kwargs["max_tokens"] == 64
    # The client default ("medium") must reach the router as a root reasoning_effort.
    assert kwargs["extra_body"] == {"reasoning_effort": "medium"}
    assert message["content"] == "ok"
    assert usage["provider"] == "selfhosted"
    assert usage["resolved_model"] == "selfhosted/my-llama-3"


def test_openai_compatible_payload_consolidates_system_messages():
    kwargs = build_remote_kwargs(
        {
            "provider": "selfhosted",
            "resolved_model": "my-llama-3",
            "usage_model": "selfhosted/my-llama-3",
        },
        [
            {"role": "system", "content": "base system"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "late runtime note"},
            {"role": "assistant", "content": "ok"},
        ],
        max_tokens=64,
        tool_choice="auto",
        temperature=None,
        tools=None,
    )

    messages = kwargs["messages"]
    assert messages[0] == {"role": "system", "content": "base system\n\nlate runtime note"}
    assert [message["role"] for message in messages] == ["system", "user", "assistant"]
    assert "extra_body" not in kwargs


def test_reasoning_effort_reaches_selfhosted_wire_but_not_cloudru():
    base = {
        "resolved_model": "my-llama-3",
        "usage_model": "selfhosted/my-llama-3",
    }
    messages = [{"role": "user", "content": "hi"}]

    selfhosted = build_remote_kwargs(
        {**base, "provider": "selfhosted"},
        messages,
        max_tokens=64,
        tool_choice="auto",
        temperature=None,
        tools=None,
        reasoning_effort="high",
    )
    assert selfhosted["extra_body"] == {"reasoning_effort": "high"}

    # Internal-only effort levels clamp to the wire set.
    clamped = build_remote_kwargs(
        {**base, "provider": "selfhosted"},
        messages,
        max_tokens=64,
        tool_choice="auto",
        temperature=None,
        tools=None,
        reasoning_effort="xhigh",
    )
    assert clamped["extra_body"] == {"reasoning_effort": "high"}

    disabled = build_remote_kwargs(
        {**base, "provider": "selfhosted"},
        messages,
        max_tokens=64,
        tool_choice="auto",
        temperature=None,
        tools=None,
        reasoning_effort="none",
    )
    assert "extra_body" not in disabled

    # "off" is the other, not-yet-unified disable word across models: same omit.
    disabled_off = build_remote_kwargs(
        {**base, "provider": "selfhosted"},
        messages,
        max_tokens=64,
        tool_choice="auto",
        temperature=None,
        tools=None,
        reasoning_effort="off",
    )
    assert "extra_body" not in disabled_off

    # Cloud.ru's reasoning contract is unconfirmed: the effort must never leak there.
    cloudru = build_remote_kwargs(
        {**base, "provider": "cloudru", "usage_model": "cloudru/my-llama-3"},
        messages,
        max_tokens=64,
        tool_choice="auto",
        temperature=None,
        tools=None,
        reasoning_effort="high",
    )
    assert "extra_body" not in cloudru


def test_selfhosted_no_auth_does_not_use_placeholder_key(monkeypatch):
    monkeypatch.setenv("SELFHOSTED_LLM_BASE_URL", "http://llm.local/v1")
    monkeypatch.delenv("SELFHOSTED_LLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key")

    target = LLMClient()._resolve_remote_target("my-llama-3")

    assert target["api_key"] == ""


def test_strip_authorization_header_removes_auth_only_for_no_auth_selfhosted():
    class _Headers(dict):
        def pop(self, key, default=None):
            return super().pop(key, default)

    request = SimpleNamespace(headers=_Headers({"Authorization": "Bearer leaked"}))

    strip_authorization_header(request, {"provider": "selfhosted", "api_key": ""})
    assert "Authorization" not in request.headers

    request_with_key = SimpleNamespace(headers=_Headers({"Authorization": "Bearer local-key"}))
    strip_authorization_header(request_with_key, {"provider": "selfhosted", "api_key": "local-key"})
    assert request_with_key.headers["Authorization"] == "Bearer local-key"

    cloudru_request = SimpleNamespace(headers=_Headers({"Authorization": "Bearer cloudru-key"}))
    strip_authorization_header(cloudru_request, {"provider": "cloudru", "api_key": ""})
    assert cloudru_request.headers["Authorization"] == "Bearer cloudru-key"


def test_selfhosted_no_auth_client_strips_sdk_authorization(monkeypatch):
    import httpx

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key")
    target = {
        "provider": "selfhosted",
        "api_key": "",
        "base_url": "http://llm.local/v1",
        "default_headers": {},
    }

    client, http_client = make_no_proxy_client(target)
    try:
        assert client.api_key != "ambient-key"
        request = httpx.Request(
            "POST",
            "http://llm.local/v1/chat/completions",
            headers={"Authorization": "Bearer unused"},
        )
        from ouroboros.observability import aef_tracing

        trace_id = "463ac35c9f6413ad48485a3953bb6124"
        with aef_tracing.trace_scope(trace_id):
            for hook in http_client.event_hooks["request"]:
                hook(request)
        assert "Authorization" not in request.headers
        assert request.headers["X-Trace-Id"] == trace_id
    finally:
        http_client.close()


def test_parse_rejects_unknown_provider_prefix_when_selfhosted_configured(monkeypatch):
    monkeypatch.setenv("SELFHOSTED_LLM_BASE_URL", "http://llm.local/v1")

    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        LLMClient()._resolve_remote_target("openai::gpt-4o-mini")


def test_runtime_does_not_qualify_plain_slots_when_selfhosted_base_set():
    settings, changed, changed_keys = apply_runtime_provider_defaults(
        AppSettings(
            SELFHOSTED_LLM_BASE_URL="http://llm.local/v1",
            CLOUDRU_FOUNDATION_MODELS_API_KEY="cloudru-key",
            OUROBOROS_MODEL="my-llama-3",
            OUROBOROS_MODEL_LIGHT="cloudru::zai-org/GLM-4.7",
            OUROBOROS_MODEL_FALLBACK="zai-org/GLM-5.1",
        )
    )

    assert changed is False
    assert changed_keys == []
    assert settings.OUROBOROS_MODEL == "my-llama-3"
    assert settings.OUROBOROS_MODEL_LIGHT == "cloudru::zai-org/GLM-4.7"
    assert settings.OUROBOROS_MODEL_FALLBACK == "zai-org/GLM-5.1"


def test_has_remote_provider_with_selfhosted_base_and_cloudru_key():
    settings = AppSettings(
        SELFHOSTED_LLM_BASE_URL="http://llm.local/v1",
        CLOUDRU_FOUNDATION_MODELS_API_KEY="cloudru-key",
        OUROBOROS_MODEL="my-llama-3",
        OUROBOROS_MODEL_LIGHT="cloudru::zai-org/GLM-4.7",
    )

    assert has_remote_provider(settings) is True


def test_explicit_cloudru_slot_still_routes_to_cloudru(monkeypatch):
    monkeypatch.setenv("SELFHOSTED_LLM_BASE_URL", "http://llm.local/v1")
    monkeypatch.setenv("CLOUDRU_FOUNDATION_MODELS_API_KEY", "cloudru-key")

    target = LLMClient()._resolve_remote_target("cloudru::zai-org/GLM-4.7")

    assert target["provider"] == "cloudru"
    assert target["resolved_model"] == "zai-org/GLM-4.7"
