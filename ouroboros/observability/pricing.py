"""
Ouroboros — LLM usage event helpers.

Infers provider/model metadata and emits llm_usage audit events (tokens and call counts).
"""

from __future__ import annotations

import logging
from typing import Any

from new_core.identity import OwnerScope, ServiceJob
from ouroboros.agent_events.interfaces import IAgentEventPublisher, LlmUsageReported
from ouroboros.contracts.owner_scope import service_identity
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
    # A version under test belongs in the bucket of the role it shadows (TN09). Checked
    # last and only while the check is on, so a stale name cannot shadow a real lane.
    if (
        settings.GIGACHAT_PREVIEW_ENABLED
        and settings.GIGACHAT_PREVIEW_MODEL
        and normalized == normalize_model_identity(settings.GIGACHAT_PREVIEW_MODEL)
    ):
        return "main"
    return "other"


def emit_llm_usage_event(
    events: IAgentEventPublisher | None,
    task_id: str,
    model: str,
    usage: dict[str, Any],
    category: str = "task",
    provider: str | None = None,
    source: str = "loop",
    *,
    scope: OwnerScope | None = None,
) -> None:
    """Report one model call's tokens and call metadata.

    ``scope`` is whose spend this was, carried on the message so the row recorded from it
    can say so. A call outside anybody's work — the instance's own diagnostic — leaves it
    out and is attributed here rather than by the consumer: the message's scope is
    required, and a consumer inventing one would be guessing whose call it was.
    """
    if events is None:
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
        events.publish(
            LlmUsageReported(
                scope=scope if scope is not None else service_identity(job=ServiceJob.RUNTIME_LOG).as_scope(),
                task_id=str(task_id or ""),
                model=str(model or ""),
                provider=resolved_provider,
                api_key_type=infer_api_key_type(model, resolved_provider),
                model_category=infer_model_category(model),
                category=str(category or "other"),
                source=str(source or ""),
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                cached_tokens=int(usage.get("cached_tokens") or 0),
                cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
                usage=dict(usage),
                ts=utc_now_iso(),
            )
        )
    except Exception:
        log.debug("Failed to report llm usage", exc_info=True)
