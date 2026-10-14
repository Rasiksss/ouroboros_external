"""infer_model_category maps configured model env vars to metric categories."""

from __future__ import annotations

from tests.helpers.parametrize import parametrize_from_dict

model_category_cases = {
    "vision model gets vision category": {
        "env": {"OUROBOROS_MODEL_VISION": "cloudru::Qwen-VL"},
        "model": "cloudru::Qwen-VL",
        "expected": "vision",
    },
    "unset vision model stays other": {
        "env": {},
        "model": "cloudru::Qwen-VL",
        "expected": "other",
    },
    "main category wins over vision for same model": {
        "env": {"OUROBOROS_MODEL": "cloudru::Qwen-VL", "OUROBOROS_MODEL_VISION": "cloudru::Qwen-VL"},
        "model": "cloudru::Qwen-VL",
        "expected": "main",
    },
}


@parametrize_from_dict(model_category_cases)
def test_model_category(monkeypatch, env: dict[str, str], model: str, expected: str) -> None:
    from ouroboros.observability.pricing import infer_model_category
    from ouroboros.storage.settings.store import reset_memory_settings_for_tests

    for key in (
        "OUROBOROS_MODEL",
        "OUROBOROS_MODEL_LIGHT",
        "OUROBOROS_MODEL_FALLBACK",
        "OUROBOROS_MODEL_VISION",
        "GIGACHAT_PREVIEW_ENABLED",
        "GIGACHAT_PREVIEW_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    reset_memory_settings_for_tests()

    assert infer_model_category(model) == expected


provider_cases = {
    "provider comes from usage stamp": {
        "model": "cloudru::Qwen-VL",
        "usage": {"prompt_tokens": 1, "provider": "cloudru"},
        "provider_kwarg": None,
        "expected": "cloudru",
    },
    "explicit provider wins over usage stamp": {
        "model": "m",
        "usage": {"provider": "cloudru"},
        "provider_kwarg": "gigachat-b2bank",
        "expected": "gigachat-b2bank",
    },
    "legacy fallback without usage stamp": {
        "model": "some-model (local)",
        "usage": {"prompt_tokens": 1},
        "provider_kwarg": None,
        "expected": "local",
    },
    "legacy fallback for regular model is openrouter": {
        "model": "some-model",
        "usage": {"prompt_tokens": 1},
        "provider_kwarg": None,
        "expected": "openrouter",
    },
}


@parametrize_from_dict(provider_cases)
def test_usage_event_provider(model: str, usage: dict, provider_kwarg: str | None, expected: str) -> None:
    from ouroboros.observability.pricing import emit_llm_usage_event
    from tests.helpers.agent_events import RecordingAgentEvents

    events = RecordingAgentEvents()
    emit_llm_usage_event(events, "t-1", model, usage, provider=provider_kwarg)

    assert events.published[0].provider == expected
