"""infer_model_category maps configured model env vars to metric categories."""

from __future__ import annotations

from ouroboros.observability.pricing import infer_model_category
from ouroboros.storage.settings.store import reset_memory_settings_for_tests


def test_vision_model_gets_vision_category(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL_VISION", "cloudru::Qwen-VL")
    reset_memory_settings_for_tests()

    assert infer_model_category("cloudru::Qwen-VL") == "vision"


def test_unset_vision_model_stays_other(monkeypatch):
    monkeypatch.delenv("OUROBOROS_MODEL_VISION", raising=False)
    reset_memory_settings_for_tests()

    assert infer_model_category("cloudru::Qwen-VL") == "other"


def test_main_category_wins_over_vision_for_same_model(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL", "cloudru::Qwen-VL")
    monkeypatch.setenv("OUROBOROS_MODEL_VISION", "cloudru::Qwen-VL")
    reset_memory_settings_for_tests()

    assert infer_model_category("cloudru::Qwen-VL") == "main"


def test_usage_event_provider_comes_from_usage_stamp():
    """emit_llm_usage_event must use the provider the client stamped into usage."""
    import queue

    from ouroboros.observability.pricing import emit_llm_usage_event

    q: queue.Queue = queue.Queue()
    emit_llm_usage_event(q, "t-1", "cloudru::Qwen-VL", {"prompt_tokens": 1, "provider": "cloudru"})

    assert q.get_nowait()["provider"] == "cloudru"


def test_usage_event_explicit_provider_wins_over_usage_stamp():
    import queue

    from ouroboros.observability.pricing import emit_llm_usage_event

    q: queue.Queue = queue.Queue()
    emit_llm_usage_event(q, "t-1", "m", {"provider": "cloudru"}, provider="gigachat-b2bank")

    assert q.get_nowait()["provider"] == "gigachat-b2bank"


def test_usage_event_legacy_fallback_without_usage_stamp():
    import queue

    from ouroboros.observability.pricing import emit_llm_usage_event

    q: queue.Queue = queue.Queue()
    emit_llm_usage_event(q, "t-1", "some-model", {"prompt_tokens": 1})
    emit_llm_usage_event(q, "t-1", "some-model (local)", {"prompt_tokens": 1})

    assert q.get_nowait()["provider"] == "openrouter"
    assert q.get_nowait()["provider"] == "local"
