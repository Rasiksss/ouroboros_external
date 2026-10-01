"""Helpers shared by server startup, onboarding, and WebSocket liveness."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ouroboros.app.config import SETTINGS_DEFAULTS
from ouroboros.llm.models import qualify
from ouroboros.llm.provider_models import CLOUDRU_DIRECT_DEFAULTS, GIGACHAT_B2BANK_DIRECT_DEFAULTS
from ouroboros.storage.settings.schema import AppSettings
from ouroboros.utils import utc_now_iso

_DIRECT_PROVIDER_AUTO_DEFAULTS = {
    "gigachat-b2bank": {
        "OUROBOROS_MODEL": GIGACHAT_B2BANK_DIRECT_DEFAULTS["main"],
        "OUROBOROS_MODEL_LIGHT": GIGACHAT_B2BANK_DIRECT_DEFAULTS["light"],
        "OUROBOROS_MODEL_FALLBACK": GIGACHAT_B2BANK_DIRECT_DEFAULTS["fallback"],
    },
    "cloudru": {
        "OUROBOROS_MODEL": CLOUDRU_DIRECT_DEFAULTS["main"],
        "OUROBOROS_MODEL_LIGHT": CLOUDRU_DIRECT_DEFAULTS["light"],
        "OUROBOROS_MODEL_FALLBACK": CLOUDRU_DIRECT_DEFAULTS["fallback"],
    },
}
_DIRECT_PROVIDER_LEGACY_DEFAULTS: dict[str, set[str]] = {
    "gigachat-b2bank": set(),
    "cloudru": set(),
}
_ALL_MODEL_SLOT_KEYS = tuple(_DIRECT_PROVIDER_AUTO_DEFAULTS["gigachat-b2bank"].keys())
_NON_CLOUDRU_MODEL_PREFIXES = (
    "gigachat-b2bank::",
    "openai::",
    "openrouter::",
    "anthropic::",
    "local::",
)


def _truthy_setting(value) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "on"}


def _settings_mapping(settings: AppSettings | dict) -> dict:
    if isinstance(settings, AppSettings):
        return settings.to_persist_dict()
    return dict(settings)


def _setting_text(settings: AppSettings | dict, key: str) -> str:
    value = settings.get(key, "")
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    return str(value or "").strip()


def _parse_model_list(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _serialize_model_list(models: list[str]) -> str:
    return ",".join(model.strip() for model in models if str(model or "").strip())


def _refresh_retired_model_defaults(settings: dict) -> tuple[dict, list[str]]:
    return dict(settings), []


def _qualify_direct_provider_model(provider: str, model: str) -> str:
    """Add provider:: to plain foundation model IDs (e.g. zai-org/GLM-5.1)."""
    return qualify(provider, model)


def _has_selfhosted_credentials(settings: AppSettings | dict) -> bool:
    return bool(_setting_text(settings, "SELFHOSTED_LLM_BASE_URL"))


def _uses_selfhosted(settings: AppSettings | dict) -> bool:
    return _has_selfhosted_credentials(settings)


def _exclusive_direct_remote_provider(settings: AppSettings | dict) -> str:
    if _has_selfhosted_credentials(settings):
        return ""
    if _uses_gigachat_b2bank(settings):
        return "gigachat-b2bank"
    if _uses_cloudru(settings):
        return "cloudru"
    return ""


def _has_gigachat_b2bank_credentials(settings: AppSettings | dict) -> bool:
    from ouroboros.llm.providers.gigachat_b2bank import gigachat_b2bank_credentials_ready

    return gigachat_b2bank_credentials_ready(settings)


def _uses_gigachat_b2bank(settings: AppSettings | dict) -> bool:
    if not _has_gigachat_b2bank_credentials(settings):
        return False
    return any(
        _setting_text(settings, key).startswith("gigachat-b2bank::")
        for key in (
            "OUROBOROS_MODEL",
            "OUROBOROS_MODEL_LIGHT",
            "OUROBOROS_MODEL_FALLBACK",
        )
    )


def _model_slots_are_defaults_or_empty(settings: AppSettings | dict) -> bool:
    return all(
        _setting_text(settings, key) in {"", _setting_text(SETTINGS_DEFAULTS, key)}
        for key in (
            "OUROBOROS_MODEL",
            "OUROBOROS_MODEL_LIGHT",
            "OUROBOROS_MODEL_FALLBACK",
        )
    )


def _uses_cloudru(settings: AppSettings | dict) -> bool:
    if not _setting_text(settings, "CLOUDRU_FOUNDATION_MODELS_API_KEY"):
        return False
    slots = [_setting_text(settings, key) for key in _ALL_MODEL_SLOT_KEYS]
    if any(slot.startswith("cloudru::") for slot in slots):
        return True
    if _has_selfhosted_credentials(settings):
        return False
    if any(
        slot.startswith(prefix)
        for slot in slots
        for prefix in _NON_CLOUDRU_MODEL_PREFIXES
        if prefix != "gigachat-b2bank::"
    ):
        return False
    if _has_gigachat_b2bank_credentials(settings):
        return any(slot.startswith("gigachat-b2bank::") for slot in slots)
    # Cloud.ru API key without GigaChat certs: use Cloud.ru even for plain model IDs
    # (zai-org/GLM-5.1) or leftover schema gigachat-b2bank:: defaults without credentials.
    return True


def classify_runtime_provider_change(
    before: AppSettings | dict,
    after: AppSettings | dict,
) -> str:
    """Classify what kind of normalization ``apply_runtime_provider_defaults`` did.

    Returns one of:

    - ``"none"`` — no change, or change was purely cosmetic.
    - ``"direct_normalize"`` — OpenRouter is NOT configured, and the function
      auto-filled direct-provider defaults.  This is the only case where a
      user-facing warning is appropriate.
    - ``"reverse_migrate"`` — OpenRouter IS configured (so no exclusive-direct
      provider is active).  ``apply_runtime_provider_defaults`` returned early
      without making any changes, so this is pure housekeeping and should NOT
      produce a warning.
    """
    provider_after = _exclusive_direct_remote_provider(after)
    if provider_after:
        return "direct_normalize"
    return "none"


def has_remote_provider(settings: AppSettings | dict) -> bool:
    """Return True when any supported remote-provider credential is configured."""
    if _uses_selfhosted(settings):
        return True
    if _uses_gigachat_b2bank(settings):
        return True
    if not _setting_text(settings, "CLOUDRU_FOUNDATION_MODELS_API_KEY"):
        return False
    slots = [_setting_text(settings, key) for key in _ALL_MODEL_SLOT_KEYS]
    if any(slot.startswith("cloudru::") for slot in slots):
        return True
    if _has_selfhosted_credentials(settings):
        return False
    return _uses_cloudru(settings)


def has_local_model_source(settings: AppSettings | dict) -> bool:
    """Return True when a local model source has been configured."""
    return False


def has_local_routing(settings: AppSettings | dict) -> bool:
    """Return True when any model slot is configured to use the local server."""
    return False


def has_startup_ready_provider(settings: AppSettings | dict) -> bool:
    """Return True when startup/onboarding should consider runtime configured."""
    # Startup should only skip onboarding when the runtime can actually serve
    # chat after boot. A local model source alone is not enough unless at least
    # one lane is routed to that local runtime.
    return has_remote_provider(settings) or has_local_routing(settings)


def has_supervisor_provider(settings: AppSettings | dict) -> bool:
    """Return True when the runtime has enough provider config to start supervisor."""
    return has_remote_provider(settings) or has_local_routing(settings)


def apply_runtime_provider_defaults(
    settings: AppSettings | dict,
) -> tuple[AppSettings, bool, list[str]]:
    """Auto-fill safe runtime defaults for the agreed provider cases."""
    normalized, retired_changed = _refresh_retired_model_defaults(_settings_mapping(settings))
    if _has_selfhosted_credentials(normalized):
        return AppSettings.model_validate(normalized), bool(retired_changed), retired_changed

    provider = _exclusive_direct_remote_provider(normalized)

    if not provider:
        return AppSettings.model_validate(normalized), bool(retired_changed), retired_changed

    changed_keys: list[str] = list(retired_changed)
    provider_defaults = _DIRECT_PROVIDER_AUTO_DEFAULTS[provider]
    for key in _ALL_MODEL_SLOT_KEYS:
        raw_current = _setting_text(normalized, key)
        current = raw_current
        default = _setting_text(SETTINGS_DEFAULTS, key)
        auto_value = provider_defaults[key]
        next_value = auto_value if current in {"", default} else current
        next_value = _qualify_direct_provider_model(provider, next_value)
        if next_value != raw_current:
            normalized[key] = next_value
            changed_keys.append(key)

    # OUROBOROS_MODEL_VISION has no auto-default (empty means "fall back to the
    # main model"), but an explicitly set plain id gets the provider prefix like
    # the other slots.
    vision_current = _setting_text(normalized, "OUROBOROS_MODEL_VISION")
    if vision_current:
        vision_next = _qualify_direct_provider_model(provider, vision_current)
        if vision_next != vision_current:
            normalized["OUROBOROS_MODEL_VISION"] = vision_next
            changed_keys.append("OUROBOROS_MODEL_VISION")

    return AppSettings.model_validate(normalized), bool(changed_keys), changed_keys


def setup_remote_if_configured(settings: AppSettings | dict, log) -> None:
    """No-op: remote git sync was removed from the headless runtime."""
    del settings, log


async def ws_heartbeat_loop(
    has_clients_fn: Callable[[], bool],
    broadcast_fn: Callable[[dict], Awaitable[None]],
    interval_sec: float = 15.0,
) -> None:
    """Keep embedded clients active and give watchdogs a steady liveness signal."""
    while True:
        await asyncio.sleep(interval_sec)
        if not has_clients_fn():
            continue
        await broadcast_fn(
            {
                "type": "heartbeat",
                "ts": utc_now_iso(),
            }
        )
