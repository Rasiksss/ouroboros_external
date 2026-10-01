"""LLM client facade for the headless runtime direct providers."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any

from new_core.llm.usage import add_usage
from new_core.tools.schema import ToolSchema
from ouroboros.llm.circuit_breaker import (
    PROVIDER_OUTAGE_MESSAGE,
    BreakerIdentity,
    LLMFailureCategory,
    classify_failure,
    failure_category_label,
    provider_base_url_for_model,
    provider_circuit_is_open,
    provider_circuit_key,
    provider_circuit_last_error,
    provider_failure_category,
    record_provider_failure,
    record_provider_success,
    reset_provider_circuit,
    reset_provider_circuit_breakers,
    resolve_provider_target_identity,
)
from ouroboros.llm.models import DEFAULT_LIGHT_MODEL, parse_model_ref
from ouroboros.llm.providers.base import sanitize_chat_completion_tools, strip_cache_control
from ouroboros.llm.registry import ProviderRegistry, get_registry
from ouroboros.llm.targets import RemoteTarget

log = logging.getLogger(__name__)

_LIMITER_LOCK = threading.Lock()
_LIMITER: tuple[int, threading.BoundedSemaphore] | None = None

__all__ = [
    "DEFAULT_LIGHT_MODEL",
    "PROVIDER_OUTAGE_MESSAGE",
    "BreakerIdentity",
    "InvalidResponse",
    "LLMClient",
    "LocalContextTooLargeError",
    "LocalLLMDeadlineError",
    "add_usage",
    "chat_with_fallback",
    "provider_base_url_for_model",
    "provider_circuit_is_open",
    "provider_circuit_key",
    "provider_circuit_last_error",
    "provider_failure_category",
    "record_provider_failure",
    "record_provider_success",
    "reset_provider_circuit",
    "reset_provider_circuit_breakers",
    "resolve_provider_target_identity",
]


class LocalContextTooLargeError(RuntimeError):
    """Raised when a local model cannot fit context without silent truncation."""


class LocalLLMDeadlineError(TimeoutError):
    """Raised when a caller deadline expires before provider transport starts."""


class InvalidResponseCategory(str, Enum):
    """Typed category for a rejected background response."""

    REJECTED = "response_rejected"
    INCOMPLETE = "incomplete_response"


@dataclass(frozen=True)
class InvalidResponse:
    """A rejected background response: a returned verdict, never raised."""

    category: InvalidResponseCategory
    detail: str

    @property
    def value(self) -> str:
        return self.category.value

    @classmethod
    def rejected(cls, detail: str) -> InvalidResponse:
        return cls(InvalidResponseCategory.REJECTED, detail)

    @classmethod
    def incomplete(cls, detail: str) -> InvalidResponse:
        return cls(InvalidResponseCategory.INCOMPLETE, detail)


def _max_llm_concurrency() -> int:
    try:
        from ouroboros.storage.settings.store import load_settings

        raw = int(load_settings().OUROBOROS_LLM_MAX_CONCURRENCY or 2)
    except Exception:
        raw = 2
    return max(1, raw)


def _llm_semaphore() -> threading.BoundedSemaphore:
    global _LIMITER
    limit = _max_llm_concurrency()
    with _LIMITER_LOCK:
        if _LIMITER is None or _LIMITER[0] != limit:
            _LIMITER = (limit, threading.BoundedSemaphore(limit))
        return _LIMITER[1]


@contextmanager
def _llm_concurrency_slot(*, deadline: float | None = None):
    sem = _llm_semaphore()
    if deadline is None:
        acquired = sem.acquire()
    else:
        remaining = deadline - time.monotonic()
        acquired = remaining > 0 and sem.acquire(timeout=remaining)
    if not acquired:
        raise LocalLLMDeadlineError("LLM deadline expired while waiting for a concurrency slot")
    try:
        yield
    finally:
        sem.release()


def _background_routing_error(failures: list[str]) -> RuntimeError:
    return RuntimeError(f"Background LLM attempts failed: {'; '.join(failures)}")


def chat_with_fallback(
    llm: Any,
    *,
    model: str,
    fallback_model: str,
    deadline: float | None,
    timeout_sec: float | None,
    read_timeout_sec: float | None = None,
    response_error: Callable[[dict[str, Any]], InvalidResponse | None] | None = None,
    on_response: Callable[[str, dict[str, Any]], None] | None = None,
    **kwargs: Any,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Call one distinct fallback with shared provider-circuit state."""
    models = [model, fallback_model] if fallback_model and fallback_model != model else [model]
    failures: list[str] = []
    seen_targets: set[str] = set()
    for index, candidate in enumerate(models):
        is_last = index == len(models) - 1
        remaining = deadline - time.monotonic() if deadline is not None else timeout_sec
        if remaining is not None and remaining <= 0:
            # The caller's deadline expired on our side, before any request was
            # sent — this is not a provider outage.
            failures.append(f"{candidate} [local_timeout/{LocalLLMDeadlineError.__name__}]")
            raise _background_routing_error(failures)
        base_url = provider_base_url_for_model(candidate)
        target_key = provider_circuit_key(candidate, base_url=base_url)
        if target_key in seen_targets:
            continue
        seen_targets.add(target_key)
        if provider_circuit_is_open(candidate, base_url=base_url):
            failures.append(f"{candidate} [provider_outage/CircuitOpen]")
            if is_last:
                raise _background_routing_error(failures)
            continue
        try:
            chat_kwargs = kwargs if deadline is None else {**kwargs, "deadline": deadline}
            phase_cap = timeout_sec
            if remaining is not None:
                phase_cap = remaining if phase_cap is None else min(phase_cap, remaining)
            message, usage = llm.chat(
                model=candidate,
                timeout_sec=phase_cap,
                read_timeout_sec=read_timeout_sec,
                **chat_kwargs,
            )
        except LocalLLMDeadlineError as exc:
            failures.append(f"{candidate} [local_timeout/{type(exc).__name__}]")
            raise _background_routing_error(failures) from None
        except Exception as exc:
            category = classify_failure(exc)
            if category == LLMFailureCategory.INTERNAL:
                raise
            label = failure_category_label(category)
            failures.append(f"{candidate} [{label}/{type(exc).__name__}]")
            if category in {LLMFailureCategory.TRANSIENT, LLMFailureCategory.INCOMPLETE}:
                record_provider_failure(candidate, base_url=base_url, error=type(exc).__name__, category=category)
            if is_last or category in {LLMFailureCategory.BAD_REQUEST, LLMFailureCategory.CONTEXT}:
                raise _background_routing_error(failures) from None
            log.info(
                "Background LLM fallback: model=%s category=%s error_type=%s", candidate, label, type(exc).__name__
            )
            continue
        if on_response is not None:
            on_response(candidate, usage)
        rejection = response_error(message) if response_error is not None else None
        if rejection is not None:
            failures.append(f"{candidate} [{rejection.value}/InvalidResponse: {rejection.detail}]")
            if rejection.category == InvalidResponseCategory.INCOMPLETE:
                record_provider_failure(
                    candidate,
                    base_url=base_url,
                    error=rejection.detail,
                    category=LLMFailureCategory.INCOMPLETE,
                )
            if is_last:
                raise _background_routing_error(failures)
            log.info("Background LLM response rejected: model=%s category=%s", candidate, rejection.value)
            continue
        record_provider_success(candidate, base_url=base_url)
        return message, usage, candidate
    raise _background_routing_error(failures)


class LLMClient:
    """LLM API wrapper delegating to registered direct-provider adapters."""

    _strip_cache_control = staticmethod(strip_cache_control)
    _sanitize_chat_completion_tools = staticmethod(sanitize_chat_completion_tools)

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "",
        registry: ProviderRegistry | None = None,
    ):
        del api_key, base_url  # reserved for tests; direct providers use env
        self._registry = registry or get_registry()

    def resolve_target(self, model: str) -> RemoteTarget:
        """Resolve model string to remote target config (for tests and diagnostics)."""
        return self._registry.resolve_target(model)

    def _resolve_remote_target(self, model: str) -> RemoteTarget:
        return self.resolve_target(model)

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[ToolSchema] | None = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 65536,
        tool_choice: str = "auto",
        use_local: bool = False,
        temperature: float | None = None,
        no_proxy: bool = False,
        timeout_sec: float | None = None,
        deadline: float | None = None,
        read_timeout_sec: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Single LLM call returning (message, usage); no_proxy avoids macOS fork proxy crashes."""
        if use_local:
            raise ValueError("Local model routing has been removed from the headless runtime.")

        adapter, target = self._registry.resolve(model)
        with _llm_concurrency_slot(deadline=deadline):
            effective_timeout = timeout_sec
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LocalLLMDeadlineError("LLM deadline expired before the provider call")
                effective_timeout = remaining if timeout_sec is None else min(timeout_sec, remaining)
            return adapter.chat(
                target,
                messages,
                tools=tools,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
                temperature=temperature,
                no_proxy=no_proxy,
                read_timeout_sec=read_timeout_sec,
                phase_cap_sec=effective_timeout,
            )

    async def chat_async(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[ToolSchema] | None = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 65536,
        tool_choice: str = "auto",
        temperature: float | None = None,
        no_proxy: bool = False,
        timeout_sec: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Async remote chat; no_proxy keeps forked macOS workers off OS proxy APIs."""
        if tools:
            raise ValueError("chat_async does not support tool calls")

        ref = parse_model_ref(model)
        adapter, target = self._registry.resolve(model)
        if ref.provider not in {"gigachat-b2bank", "cloudru", "selfhosted"}:
            raise ValueError(
                "Only GigaChat/B2Bank, Cloud.ru, and self-hosted OpenAI-compatible LLMs "
                "are supported in the headless runtime."
            )
        sem = _llm_semaphore()
        await asyncio.to_thread(sem.acquire)
        try:
            return await asyncio.to_thread(
                adapter.chat,
                target,
                messages,
                tools=tools,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
                temperature=temperature,
                no_proxy=no_proxy,
                phase_cap_sec=timeout_sec,
            )
        finally:
            sem.release()

    def vision_query(
        self,
        prompt: str,
        images: list[dict[str, Any]],
        model: str = DEFAULT_LIGHT_MODEL,
        max_tokens: int = 32768,
        reasoning_effort: str = "none",
    ) -> tuple[str, dict[str, Any]]:
        """Run a lightweight vision query; image dicts use url or base64+mime."""
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            if "url" in img:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": img["url"]},
                    }
                )
            elif "base64" in img:
                mime = img.get("mime", "image/png")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{img['base64']}"},
                    }
                )
            else:
                log.warning("vision_query: skipping image with unknown format: %s", list(img.keys()))

        messages = [{"role": "user", "content": content}]
        response_msg, usage = self.chat(
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
        )
        text = response_msg.get("content") or ""
        return text, usage

    def default_model(self) -> str:
        """Return the single default model from config."""
        # Local import: settings schema imports the llm package, so a top-level
        # import back into settings would be circular.
        from ouroboros.storage.settings.user_settings import SETTINGS_OWNER_ID, user_settings

        return user_settings.OUROBOROS_MODEL(SETTINGS_OWNER_ID) or "gigachat-b2bank::GigaChat-Pro"
