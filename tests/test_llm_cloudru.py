from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from new_core.tools.schema import ToolSchema
from ouroboros.app.server_runtime import apply_runtime_provider_defaults, has_remote_provider
from ouroboros.llm.client import LLMClient
from ouroboros.llm.models import parse_model_ref
from ouroboros.llm.providers.base import ProviderResponseError
from ouroboros.llm.providers.openai_compat import OpenAICompatibleProvider
from ouroboros.storage.settings.schema import AppSettings


def test_resolve_cloudru_target_uses_default_base_url(monkeypatch):
    monkeypatch.setenv("CLOUDRU_FOUNDATION_MODELS_API_KEY", "cloudru-key")
    monkeypatch.delenv("CLOUDRU_FOUNDATION_MODELS_BASE_URL", raising=False)

    target = LLMClient()._resolve_remote_target("cloudru::zai-org/GLM-4.7")

    assert target["provider"] == "cloudru"
    assert target["api_key"] == "cloudru-key"
    assert target["base_url"] == "https://foundation-models.api.cloud.ru/v1"
    assert target["resolved_model"] == "zai-org/GLM-4.7"
    assert target["usage_model"] == "cloudru/zai-org/GLM-4.7"


def test_cloudru_chat_uses_openai_compatible_payload(monkeypatch):
    monkeypatch.setenv("CLOUDRU_FOUNDATION_MODELS_API_KEY", "cloudru-key")
    client = LLMClient()
    seen: dict[str, object] = {}

    class _FakeResponse:
        def model_dump(self):
            return {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            }

    def create(**kwargs):
        seen["kwargs"] = kwargs
        return _FakeResponse()

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider = OpenAICompatibleProvider("cloudru")
    monkeypatch.setattr(provider, "_get_remote_client", lambda target: fake_client)

    def _resolve(model: str):
        ref = parse_model_ref(model)
        return provider, provider.resolve_target(ref)

    monkeypatch.setattr(client._registry, "resolve", _resolve)

    message, usage = client.chat(
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}],
            }
        ],
        model="cloudru::zai-org/GLM-4.7",
        tools=[ToolSchema(name="echo_tool", description="Echo", parameters={})],
        max_tokens=128,
    )

    kwargs = seen["kwargs"]
    assert kwargs["model"] == "zai-org/GLM-4.7"
    assert kwargs["max_tokens"] == 128
    assert "extra_body" not in kwargs
    assert "cache_control" not in kwargs["messages"][0]["content"][0]
    assert "cache_control" not in kwargs["tools"][0]
    assert message["content"] == "ok"
    assert usage["provider"] == "cloudru"
    assert usage["resolved_model"] == "cloudru/zai-org/GLM-4.7"


@pytest.mark.parametrize(
    "outcome",
    [
        "not-a-response",
        json.JSONDecodeError("invalid", "x", 0),
        SimpleNamespace(model_dump=lambda: {"choices": {"unexpected": "object"}}),
        # Regression: a non-dict choice used to leak AttributeError (INTERNAL, no fallback).
        SimpleNamespace(model_dump=lambda: {"choices": ["oops"]}),
    ],
)
def test_cloudru_malformed_response_is_provider_error(monkeypatch, outcome):
    def create(**_kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    provider = OpenAICompatibleProvider("cloudru")
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(provider, "_get_remote_client", lambda _target: fake_client)

    with pytest.raises(ProviderResponseError):
        provider.chat(
            {"provider": "cloudru", "resolved_model": "model"},
            [{"role": "user", "content": "hi"}],
            tools=None,
            reasoning_effort="none",
            max_tokens=32,
            tool_choice="auto",
            temperature=None,
            no_proxy=False,
        )


def test_runtime_defaults_switch_to_cloudru_with_only_cloudru_credentials():
    settings, changed, changed_keys = apply_runtime_provider_defaults(
        AppSettings(CLOUDRU_FOUNDATION_MODELS_API_KEY="cloudru-key")
    )

    assert changed is True
    assert set(changed_keys) >= {
        "OUROBOROS_MODEL",
        "OUROBOROS_MODEL_LIGHT",
        "OUROBOROS_MODEL_FALLBACK",
    }
    assert settings.OUROBOROS_MODEL.startswith("cloudru::")
    assert settings.OUROBOROS_MODEL_LIGHT.startswith("cloudru::")
    assert has_remote_provider(settings) is True


def test_runtime_does_not_treat_cloudru_key_as_provider_for_explicit_other_model():
    settings = AppSettings(
        CLOUDRU_FOUNDATION_MODELS_API_KEY="cloudru-key",
        OUROBOROS_MODEL="openai::gpt-4o-mini",
    )

    assert has_remote_provider(settings) is False


def test_runtime_qualifies_plain_foundation_model_ids_for_cloudru():
    settings, changed, changed_keys = apply_runtime_provider_defaults(
        AppSettings(
            CLOUDRU_FOUNDATION_MODELS_API_KEY="cloudru-key",
            OUROBOROS_MODEL="",
            OUROBOROS_MODEL_LIGHT="zai-org/GLM-5.1",
            OUROBOROS_MODEL_FALLBACK="zai-org/GLM-5.1",
        )
    )

    assert changed is True
    assert settings.OUROBOROS_MODEL.startswith("cloudru::")
    assert settings.OUROBOROS_MODEL_LIGHT == "cloudru::zai-org/GLM-5.1"
    assert settings.OUROBOROS_MODEL_FALLBACK == "cloudru::zai-org/GLM-5.1"
    assert "OUROBOROS_MODEL_LIGHT" in changed_keys


def test_runtime_qualifies_plain_vision_model_but_keeps_empty_untouched():
    settings, changed, changed_keys = apply_runtime_provider_defaults(
        AppSettings(
            CLOUDRU_FOUNDATION_MODELS_API_KEY="cloudru-key",
            OUROBOROS_MODEL_VISION="Qwen2.5-VL",
        )
    )

    assert changed is True
    assert settings.OUROBOROS_MODEL_VISION == "cloudru::Qwen2.5-VL"
    assert "OUROBOROS_MODEL_VISION" in changed_keys

    settings, _, changed_keys = apply_runtime_provider_defaults(
        AppSettings(CLOUDRU_FOUNDATION_MODELS_API_KEY="cloudru-key")
    )

    assert settings.OUROBOROS_MODEL_VISION == ""
    assert "OUROBOROS_MODEL_VISION" not in changed_keys


def test_cloudru_key_with_plain_foundation_model_ids_enables_supervisor():
    from ouroboros.app.server_runtime import has_supervisor_provider

    settings = AppSettings(
        CLOUDRU_FOUNDATION_MODELS_API_KEY="cloudru-key",
        OUROBOROS_MODEL="zai-org/GLM-5.1",
        OUROBOROS_MODEL_LIGHT="zai-org/GLM-5.1",
        OUROBOROS_MODEL_FALLBACK="zai-org/GLM-5.1",
    )

    assert has_remote_provider(settings) is True
    assert has_supervisor_provider(settings) is True
