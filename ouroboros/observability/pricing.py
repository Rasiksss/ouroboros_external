"""
Ouroboros — LLM usage event helpers.

Infers provider/model metadata and emits llm_usage audit events (tokens and call counts).
"""

from __future__ import annotations

import logging
import queue
from typing import Any

from ouroboros.llm.provider_models import normalize_model_identity
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)


def infer_api_key_type(model: str, provider: str | None = None) -> str:
    """Infer which API key is used based on model name."""
    provider_name = str(provider or "").strip().lower()
    if provider_name in {
        "local",
        "openrouter",
        "openai",
        "anthropic",
        "openai-compatible",
        "cloudru",
        "gigachat-b2bank",
        "selfhosted",
    }:
        return provider_name
    raw_model = str(model or "").strip()
    if raw_model.endswith(" (local)"):
        return "local"
    if raw_model.startswith("openai::"):
        return "openai"
    if raw_model.startswith("anthropic::"):
        return "anthropic"
    if raw_model.startswith("openai-compatible::"):
        return "openai-compatible"
    if raw_model.startswith("cloudru::"):
        return "cloudru"
    if raw_model.startswith("gigachat-b2bank::"):
        return "gigachat-b2bank"
    from ouroboros.storage.settings.store import load_settings

    if "::" not in raw_model and str(load_settings().SELFHOSTED_LLM_BASE_URL or "").strip():
        return "selfhosted"
    normalized = normalize_model_identity(raw_model)
    if str(model or "").endswith(" (local)"):
        return "local"
    if normalized.startswith("openai/"):
        return "openrouter"
    if normalized.startswith("openai-compatible/"):
        return "openai-compatible"
    if normalized.startswith("cloudru/"):
        return "cloudru"
    if normalized.startswith("gigachat-b2bank/"):
        return "gigachat-b2bank"
    if normalized.startswith("selfhosted/"):
        return "selfhosted"
    if normalized.startswith(("anthropic/", "google/", "openai/", "x-ai/", "qwen/")):
        return "openrouter"
    if "claude" in normalized.lower():
        return "anthropic"
    return "openrouter"


def infer_provider_from_model(model: str) -> str:
    """Derive the provider string from a model identifier."""
    raw = str(model or "").strip()
    if raw.endswith(" (local)"):
        raw = raw[:-8]
    if raw.startswith("anthropic::"):
        return "anthropic"
    if raw.startswith("openai::"):
        return "openai"
    if raw.startswith("openai-compatible::"):
        return "openai-compatible"
    if raw.startswith("cloudru::"):
        return "cloudru"
    if raw.startswith("gigachat-b2bank::"):
        return "gigachat-b2bank"
    from ouroboros.storage.settings.store import load_settings

    if "::" not in raw and str(load_settings().SELFHOSTED_LLM_BASE_URL or "").strip():
        return "selfhosted"
    return "openrouter"


def infer_model_category(model: str) -> str:
    """Infer model category by comparing against configured model env vars."""
    from ouroboros.storage.settings.store import load_settings

    model = str(model or "").strip()
    if model.endswith(" (local)"):
        model = model[:-8]
    normalized = normalize_model_identity(model)
    settings = load_settings()
    configured = {
        "main": settings.OUROBOROS_MODEL or "",
        "light": settings.OUROBOROS_MODEL_LIGHT or "",
        "fallback": settings.OUROBOROS_MODEL_FALLBACK or "",
        "vision": settings.OUROBOROS_MODEL_VISION or "",
    }
    for cat, val in configured.items():
        if val and normalized == normalize_model_identity(val):
            return cat
    return "other"


def emit_llm_usage_event(
    event_queue: queue.Queue | None,
    task_id: str,
    model: str,
    usage: dict[str, Any],
    category: str = "task",
    provider: str | None = None,
    source: str = "loop",
) -> None:
    """Emit llm_usage event to the event queue (tokens and call metadata only)."""
    if not event_queue:
        return
    try:
        # The client stamps the real provider into usage on every call
        # (llm/providers/base.py); the openrouter/local guess is a legacy
        # fallback for usage dicts that predate that stamp.
        resolved_provider = (
            str(provider or "").strip()
            or str(usage.get("provider") or "").strip()
            or ("local" if str(model or "").endswith(" (local)") else "openrouter")
        )
        event_queue.put_nowait(
            {
                "type": "llm_usage",
                "ts": utc_now_iso(),
                "task_id": task_id,
                "model": model,
                "api_key_type": infer_api_key_type(model, resolved_provider),
                "model_category": infer_model_category(model),
                "provider": resolved_provider,
                "source": source,
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "cached_tokens": int(usage.get("cached_tokens") or 0),
                "cache_write_tokens": int(usage.get("cache_write_tokens") or 0),
                "usage": usage,
                "category": category,
            }
        )
    except Exception:
        log.debug("Failed to put llm_usage event to queue", exc_info=True)
