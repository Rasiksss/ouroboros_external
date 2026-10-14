"""LLM client facade for the headless runtime direct providers."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypedDict

from new_core.coordination.registry import MODEL_CALLS, concurrency_budgets
from new_core.identity import OwnerScope
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
from ouroboros.llm.models import DEFAULT_LIGHT_MODEL
from ouroboros.llm.providers.base import sanitize_chat_completion_tools, strip_cache_control
from ouroboros.llm.registry import ProviderRegistry, get_registry
from ouroboros.llm.targets import RemoteTarget
from ouroboros.storage.task_results import ExecutionClaimLost, check_execution_claim

log = logging.getLogger(__name__)

#: How long a grant outlives the call that holds it when the holder never releases —
#: above any single call's own deadline, because a lease that expires under a live holder
#: over-admits the budget it was meant to bound.
_MODEL_CALL_LEASE_TTL_SEC = 3600.0

__all__ = [
    "DEFAULT_LIGHT_MODEL",
    "PROVIDER_OUTAGE_MESSAGE",
    "BreakerIdentity",
    "InvalidResponse",
    "LLMCallCancelled",
    "LLMClient",
    "LocalContextTooLargeError",
    "LocalLLMDeadlineError",
    "add_usage",
    "async_chat_with_fallback",
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


class LLMCallCancelled(RuntimeError):
    """Raised when a task stop prevents provider dispatch."""


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


def _model_call_holder() -> str:
    """Who the lease names: the call, not the thread it started on.

    Under an event loop many concurrent calls share one thread, so a thread identifier names
    every one of them alike and the budget can no longer tell one holder's release from
    another's. A fresh identity per call is the smallest thing that is true on both paths.
    """
    return f"llm-{uuid.uuid4().hex}"


def _slot_wait(poll_interval: float, deadline: float | None) -> float:
    return poll_interval if deadline is None else min(poll_interval, max(0.0, deadline - time.monotonic()))


def _before_dispatch(stop_requested: Callable[[], bool] | None) -> None:
    if stop_requested is not None and stop_requested():
        raise LLMCallCancelled("Task stopped before model dispatch")
    check_execution_claim()


@contextmanager
def _llm_concurrency_slot(*, deadline: float | None = None, stop_requested: Callable[[], bool] | None = None):
    """One grant from the process's model-call budget, held for the call.

    A budget rather than a semaphore in this module: the count belongs to the deployment
    once there is more than one instance, and the seam moves to a shared backend without
    this caller changing. A caller with a deadline gives up on it rather than waiting —
    which is what keeps the supervisor's own diagnostic call from stalling its tick behind
    the runs.
    """
    from ouroboros.storage.settings.store import load_settings

    budgets = concurrency_budgets.budgets
    poll_interval = load_settings().OUROBOROS_LLM_CLAIM_POLL_INTERVAL_SEC
    holder = _model_call_holder()
    lease = None
    try:
        while True:
            _before_dispatch(stop_requested)
            lease = budgets.acquire(
                MODEL_CALLS,
                holder=holder,
                ttl_sec=_MODEL_CALL_LEASE_TTL_SEC,
                timeout_sec=_slot_wait(poll_interval, deadline),
            )
            if lease is not None:
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise LocalLLMDeadlineError("LLM deadline expired while waiting for a concurrency slot")
        check_execution_claim(model_start=True)
        if stop_requested is not None and stop_requested():
            raise LLMCallCancelled("Task stopped before model dispatch")
        yield
    except ExecutionClaimLost as exc:
        raise LLMCallCancelled(str(exc)) from exc
    finally:
        if lease is not None:
            budgets.release(lease)


@asynccontextmanager
async def _async_llm_concurrency_slot(
    *, deadline: float | None = None, stop_requested: Callable[[], bool] | None = None
) -> AsyncIterator[None]:
    """:func:`_llm_concurrency_slot` for a caller that must not hold its event loop.

    The same slot from the same budget — an asynchronous path outside it would have no
    ceiling at all — waited for through the budget's awaitable acquisition, so the
    deliveries that want nothing from it keep running while this one waits.
    """
    from ouroboros.storage.settings.store import load_settings

    budgets = concurrency_budgets.budgets
    poll_interval = load_settings().OUROBOROS_LLM_CLAIM_POLL_INTERVAL_SEC
    holder = _model_call_holder()
    lease = None
    try:
        while True:
            _before_dispatch(stop_requested)
            lease = await budgets.acquire_async(
                MODEL_CALLS,
                holder=holder,
                ttl_sec=_MODEL_CALL_LEASE_TTL_SEC,
                timeout_sec=_slot_wait(poll_interval, deadline),
            )
            if lease is not None:
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise LocalLLMDeadlineError("LLM deadline expired while waiting for a concurrency slot")
        check_execution_claim(model_start=True)
        if stop_requested is not None and stop_requested():
            raise LLMCallCancelled("Task stopped before model dispatch")
        yield
    except ExecutionClaimLost as exc:
        raise LLMCallCancelled(str(exc)) from exc
    finally:
        if lease is not None:
            budgets.release(lease)


def _background_routing_error(failures: list[str]) -> RuntimeError:
    return RuntimeError(f"Background LLM attempts failed: {'; '.join(failures)}")


@dataclass(frozen=True)
class _Attempt:
    """One candidate the routing asks the caller to dispatch."""

    model: str
    phase_cap: float | None


@dataclass(frozen=True)
class _Answer:
    message: dict[str, Any]
    usage: dict[str, Any]


@dataclass(frozen=True)
class _Fault:
    error: BaseException


def _route_with_fallback(
    *,
    model: str,
    fallback_model: str,
    deadline: float | None,
    timeout_sec: float | None,
    response_error: Callable[[dict[str, Any]], InvalidResponse | None] | None,
    on_response: Callable[[str, dict[str, Any]], None] | None,
) -> Generator[_Attempt, _Answer | _Fault | None, tuple[dict[str, Any], dict[str, Any], str]]:
    """Every routing decision of one call with one distinct fallback, transport aside.

    Declared once and driven by both entry points: which candidates are tried and in what
    order, the shared deadline the fallback inherits what remains of, the provider circuit
    state, the failure classification, the response validation and the token accounting.
    The caller dispatches the yielded attempt and sends back its answer or its fault, so
    the only thing the two paths do differently is wait.
    """
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
        phase_cap = timeout_sec
        if remaining is not None:
            phase_cap = remaining if phase_cap is None else min(phase_cap, remaining)
        outcome = yield _Attempt(model=candidate, phase_cap=phase_cap)
        if isinstance(outcome, _Fault):
            exc = outcome.error
            if isinstance(exc, LocalLLMDeadlineError):
                failures.append(f"{candidate} [local_timeout/{type(exc).__name__}]")
                raise _background_routing_error(failures)
            category = classify_failure(exc)
            if category == LLMFailureCategory.INTERNAL:
                raise exc
            label = failure_category_label(category)
            failures.append(f"{candidate} [{label}/{type(exc).__name__}]")
            if category in {LLMFailureCategory.TRANSIENT, LLMFailureCategory.INCOMPLETE}:
                record_provider_failure(candidate, base_url=base_url, error=type(exc).__name__, category=category)
            if is_last or category in {LLMFailureCategory.BAD_REQUEST, LLMFailureCategory.CONTEXT}:
                raise _background_routing_error(failures)
            log.info(
                "Background LLM fallback: model=%s category=%s error_type=%s", candidate, label, type(exc).__name__
            )
            continue
        if not isinstance(outcome, _Answer):
            raise RuntimeError("the model routing was driven without an outcome")
        if on_response is not None:
            on_response(candidate, outcome.usage)
        rejection = response_error(outcome.message) if response_error is not None else None
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
        return outcome.message, outcome.usage, candidate
    raise _background_routing_error(failures)


def chat_with_fallback(
    llm: LLMClient,
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
    route = _route_with_fallback(
        model=model,
        fallback_model=fallback_model,
        deadline=deadline,
        timeout_sec=timeout_sec,
        response_error=response_error,
        on_response=on_response,
    )
    chat_kwargs = kwargs if deadline is None else {**kwargs, "deadline": deadline}
    outcome: _Answer | _Fault | None = None
    while True:
        try:
            attempt = route.send(outcome)
        except StopIteration as stop:
            return stop.value
        try:
            message, usage = llm.chat(
                model=attempt.model,
                timeout_sec=attempt.phase_cap,
                read_timeout_sec=read_timeout_sec,
                **chat_kwargs,
            )
        except Exception as exc:
            outcome = _Fault(exc)
        else:
            outcome = _Answer(message, usage)


async def async_chat_with_fallback(
    llm: LLMClient,
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
    """:func:`chat_with_fallback` without occupying the caller's event loop."""
    route = _route_with_fallback(
        model=model,
        fallback_model=fallback_model,
        deadline=deadline,
        timeout_sec=timeout_sec,
        response_error=response_error,
        on_response=on_response,
    )
    chat_kwargs = kwargs if deadline is None else {**kwargs, "deadline": deadline}
    outcome: _Answer | _Fault | None = None
    while True:
        try:
            attempt = route.send(outcome)
        except StopIteration as stop:
            return stop.value
        try:
            message, usage = await llm.async_chat(
                model=attempt.model,
                timeout_sec=attempt.phase_cap,
                read_timeout_sec=read_timeout_sec,
                **chat_kwargs,
            )
        except Exception as exc:
            outcome = _Fault(exc)
        else:
            outcome = _Answer(message, usage)


class VisionImage(TypedDict, total=False):
    url: str
    base64: str
    mime: str


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
        stop_requested: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Single LLM call returning (message, usage); no_proxy avoids macOS fork proxy crashes."""
        if use_local:
            raise ValueError("Local model routing has been removed from the headless runtime.")

        adapter, target = self._registry.resolve(model)
        with _llm_concurrency_slot(deadline=deadline, stop_requested=stop_requested):
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

    async def async_chat(
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
        stop_requested: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """:meth:`chat` without occupying the caller's event loop. Tools are carried."""
        if use_local:
            raise ValueError("Local model routing has been removed from the headless runtime.")

        adapter, target = self._registry.resolve(model)
        async with _async_llm_concurrency_slot(deadline=deadline, stop_requested=stop_requested):
            effective_timeout = timeout_sec
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LocalLLMDeadlineError("LLM deadline expired before the provider call")
                effective_timeout = remaining if timeout_sec is None else min(timeout_sec, remaining)
            return await adapter.async_chat(
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

    def vision_query(
        self,
        prompt: str,
        images: list[VisionImage],
        model: str = DEFAULT_LIGHT_MODEL,
        max_tokens: int = 32768,
        reasoning_effort: str = "none",
        stop_requested: Callable[[], bool] | None = None,
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
            stop_requested=stop_requested,
        )
        text = response_msg.get("content") or ""
        return text, usage

    def default_model(self, scope: OwnerScope) -> str:
        """Return the default model in force for this scope."""
        # Local import: settings schema imports the llm package, so a top-level
        # import back into settings would be circular.
        from config_registry.user_settings.reader import user_settings_factory

        return user_settings_factory.settings().OUROBOROS_MODEL(scope) or "gigachat-b2bank::GigaChat-Pro"
