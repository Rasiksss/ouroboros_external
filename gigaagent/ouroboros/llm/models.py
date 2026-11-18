"""Model ID parsing, qualification, and identity normalization (SSOT)."""

from __future__ import annotations

from dataclasses import dataclass

DIRECT_PROVIDERS = frozenset({"gigachat-b2bank", "cloudru", "selfhosted"})
DEFAULT_LIGHT_MODEL = "gigachat-b2bank::GigaChat"


def selfhosted_base_url_configured() -> bool:
    from ouroboros.storage.settings.store import load_settings

    return bool((load_settings().SELFHOSTED_LLM_BASE_URL or "").strip())


@dataclass(frozen=True, slots=True)
class ModelRef:
    """Parsed model reference: provider lane + bare model id."""

    provider: str
    model_id: str

    @property
    def usage_model(self) -> str:
        return usage_model_name(self.provider, self.model_id)

    @property
    def prefixed(self) -> str:
        return format_prefixed(self.provider, self.model_id)

    def __str__(self) -> str:
        return self.prefixed


def usage_model_name(provider: str, model_id: str) -> str:
    if provider == "gigachat-b2bank":
        return f"gigachat-b2bank/{model_id}"
    if provider == "cloudru":
        return f"cloudru/{model_id}"
    if provider == "selfhosted":
        return f"selfhosted/{model_id}"
    return model_id


def format_prefixed(provider: str, model_id: str) -> str:
    return f"{provider}::{model_id}"


def provider_prefix(provider: str) -> str:
    return f"{provider}::"


def has_provider_prefix(value: str) -> bool:
    return "::" in str(value or "").strip()


def qualify(provider: str, model: str) -> str:
    """Add ``provider::`` to plain foundation model IDs when needed."""
    text = str(model or "").strip()
    if provider == "selfhosted" or not text or has_provider_prefix(text):
        return text
    return format_prefixed(provider, text)


def migrate_model_value(provider: str, value: str) -> str:
    text = str(value or "").strip()
    if provider == "gigachat-b2bank" and text.startswith("gigachat-b2bank/"):
        return format_prefixed(provider, text[len("gigachat-b2bank/") :])
    if provider == "cloudru" and text.startswith("cloudru/"):
        return format_prefixed(provider, text[len("cloudru/") :])
    return text


def normalize_model_identity(model: str) -> str:
    text = str(model or "").strip()
    if text.endswith(" (local)"):
        text = text[:-8]
    if text.startswith("gigachat-b2bank::"):
        return f"gigachat-b2bank/{text[len('gigachat-b2bank::') :]}"
    if text.startswith("cloudru::"):
        return f"cloudru/{text[len('cloudru::') :]}"
    if text.startswith("selfhosted/"):
        return text[len("selfhosted/") :]
    return text


def parse_model_ref(model: str) -> ModelRef:
    """Parse env/runtime model strings into a :class:`ModelRef`."""
    model_name = str(model or "").strip()
    if model_name.startswith("gigachat-b2bank::"):
        return ModelRef("gigachat-b2bank", model_name[len("gigachat-b2bank::") :].strip())
    if model_name.startswith("gigachat-b2bank/"):
        return ModelRef("gigachat-b2bank", model_name[len("gigachat-b2bank/") :].strip())
    if model_name.startswith("cloudru::"):
        return ModelRef("cloudru", model_name[len("cloudru::") :].strip())
    if model_name.startswith("cloudru/"):
        return ModelRef("cloudru", model_name[len("cloudru/") :].strip())
    if "::" in model_name:
        raise ValueError(f"Unsupported LLM provider for headless runtime: {model_name!r}")
    if not model_name:
        if selfhosted_base_url_configured():
            from ouroboros.storage.settings.store import load_settings

            fallback = (load_settings().OUROBOROS_MODEL or "").strip()
            if fallback and not has_provider_prefix(fallback):
                return ModelRef("selfhosted", fallback)
            raise ValueError("Model required for self-hosted LLM routing")
        return ModelRef("gigachat-b2bank", "GigaChat")
    if selfhosted_base_url_configured():
        return ModelRef("selfhosted", model_name)
    raise ValueError(f"Unsupported LLM provider for headless runtime: {model_name!r}")
