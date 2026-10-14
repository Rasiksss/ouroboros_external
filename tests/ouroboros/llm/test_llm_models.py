from __future__ import annotations

import pytest

from ouroboros.llm.models import (
    ModelRef,
    format_prefixed,
    has_provider_prefix,
    migrate_model_value,
    normalize_model_identity,
    parse_model_ref,
    qualify,
    usage_model_name,
)


def test_parse_cloudru_prefixed():
    ref = parse_model_ref("cloudru::zai-org/GLM-4.7")
    assert ref == ModelRef("cloudru", "zai-org/GLM-4.7")
    assert ref.prefixed == "cloudru::zai-org/GLM-4.7"
    assert ref.usage_model == "cloudru/zai-org/GLM-4.7"


def test_parse_legacy_slash_prefix():
    ref = parse_model_ref("gigachat-b2bank/GigaChat-Pro")
    assert ref.provider == "gigachat-b2bank"
    assert ref.model_id == "GigaChat-Pro"


def test_qualify_adds_provider_prefix():
    assert qualify("cloudru", "zai-org/GLM-5.1") == "cloudru::zai-org/GLM-5.1"
    assert qualify("cloudru", "cloudru::already") == "cloudru::already"
    assert qualify("selfhosted", "my-model") == "my-model"


def test_migrate_model_value():
    assert migrate_model_value("cloudru", "cloudru/foo") == "cloudru::foo"


def test_normalize_model_identity():
    assert normalize_model_identity("cloudru::zai-org/GLM-4.7") == "cloudru/zai-org/GLM-4.7"
    assert normalize_model_identity("selfhosted/my-llama") == "my-llama"


def test_parse_selfhosted_plain_id(monkeypatch):
    monkeypatch.setenv("SELFHOSTED_LLM_BASE_URL", "http://llm.local/v1")
    ref = parse_model_ref("my-llama-3")
    assert ref.provider == "selfhosted"
    assert ref.model_id == "my-llama-3"


def test_parse_rejects_unknown_prefix(monkeypatch):
    monkeypatch.setenv("SELFHOSTED_LLM_BASE_URL", "http://llm.local/v1")
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        parse_model_ref("openai::gpt-4o-mini")


def test_has_provider_prefix():
    assert has_provider_prefix("cloudru::x") is True
    assert has_provider_prefix("plain-id") is False


def test_format_and_usage_model():
    assert format_prefixed("cloudru", "m") == "cloudru::m"
    assert usage_model_name("gigachat-b2bank", "GigaChat") == "gigachat-b2bank/GigaChat"
