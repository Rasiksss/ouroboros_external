"""Direct-provider model ID helpers and runtime defaults."""

from __future__ import annotations

import re

from ouroboros.llm.models import (
    DEFAULT_LIGHT_MODEL,
    migrate_model_value,
    normalize_model_identity,
    qualify,
)

GIGACHAT_B2BANK_DIRECT_DEFAULTS = {
    "main": "gigachat-b2bank::GigaChat-Pro",
    "light": "gigachat-b2bank::GigaChat",
    "fallback": "gigachat-b2bank::GigaChat",
}

CLOUDRU_FOUNDATION_MODELS_BASE_URL = "https://foundation-models.api.cloud.ru/v1"
CLOUDRU_DIRECT_DEFAULTS = {
    "main": "cloudru::zai-org/GLM-4.7",
    "light": "cloudru::zai-org/GLM-4.7",
    "fallback": "cloudru::zai-org/GLM-4.7",
}


_VERSIONED_API_SUFFIX_RE = re.compile(r"/v\d+$")


def normalize_openai_compatible_base_url(base_url: str) -> str:
    """Ensure OpenAI SDK base URLs end with ``/v1`` (ModelRun, vLLM, etc.)."""
    text = str(base_url or "").strip().rstrip("/")
    if not text:
        return ""
    if _VERSIONED_API_SUFFIX_RE.search(text):
        return text
    return f"{text}/v1"


def compute_direct_review_models_fallback(
    provider: str,
    main_model: str,
    light_model: str = "",
    *,
    review_runs: int = 3,
) -> list[str]:
    """Return direct-provider review fallback preserving commit-triad shape."""
    provider_defaults = {
        "gigachat-b2bank": GIGACHAT_B2BANK_DIRECT_DEFAULTS,
        "cloudru": CLOUDRU_DIRECT_DEFAULTS,
    }.get(provider)
    if not provider_defaults:
        return []
    provider_prefix = f"{provider}::"
    main = migrate_model_value(provider, main_model)
    if not main.startswith(provider_prefix):
        return []
    light = migrate_model_value(provider, light_model) if light_model else ""
    default_light = migrate_model_value(provider, provider_defaults.get("light", ""))
    light_slot = light if light.startswith(provider_prefix) else default_light
    if light_slot and light_slot != main:
        return [main, light_slot, light_slot]
    return [main] * int(review_runs or 3)


def get_model_context_tokens(default: int = 200_000) -> int:
    """Return the context window size in tokens.

    Reads OUROBOROS_MODEL_CONTEXT_TOKENS env var; falls back to ``default`` (131k).
    """
    from ouroboros.storage.settings.store import load_settings

    env_val = str(load_settings().OUROBOROS_MODEL_CONTEXT_TOKENS or "").strip()
    if env_val:
        try:
            parsed = int(env_val)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return default


__all__ = [
    "CLOUDRU_DIRECT_DEFAULTS",
    "CLOUDRU_FOUNDATION_MODELS_BASE_URL",
    "DEFAULT_LIGHT_MODEL",
    "GIGACHAT_B2BANK_DIRECT_DEFAULTS",
    "compute_direct_review_models_fallback",
    "get_model_context_tokens",
    "migrate_model_value",
    "normalize_model_identity",
    "normalize_openai_compatible_base_url",
    "qualify",
]
