"""Headless LLM client boundary."""

from ouroboros.llm.client import (
    DEFAULT_LIGHT_MODEL,
    InvalidResponse,
    LLMClient,
    LocalContextTooLargeError,
    LocalLLMDeadlineError,
    add_usage,
    chat_with_fallback,
)
from ouroboros.llm.models import ModelRef, parse_model_ref, qualify
from ouroboros.llm.registry import ProviderRegistry, get_registry

__all__ = [
    "DEFAULT_LIGHT_MODEL",
    "InvalidResponse",
    "LLMClient",
    "LocalContextTooLargeError",
    "LocalLLMDeadlineError",
    "ModelRef",
    "ProviderRegistry",
    "add_usage",
    "chat_with_fallback",
    "get_registry",
    "parse_model_ref",
    "qualify",
]
