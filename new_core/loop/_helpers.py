"""Pure loop value helpers; shared predicates are exported through new_core.loop."""

from __future__ import annotations

from new_core.llm.types import LLMFailure

MODEL_UNAVAILABLE_MESSAGE = (
    "⚠️ Выбранная версия модели GigaChat временно недоступна по техническим причинам. Попробуйте позже."
)


def is_model_unavailable(failure: LLMFailure | None) -> bool:
    """Whether the loop recorded the provider's model-unavailable stop signal."""
    return failure is not None and failure.model_unavailable


def failure_message(failure: LLMFailure | None, default: str, *, model_unavailable: bool = False) -> str:
    return (failure.user_message if failure else "") or (MODEL_UNAVAILABLE_MESSAGE if model_unavailable else default)


def is_token_overflow(failure: LLMFailure | None) -> bool:
    """Whether the provider classified the failure as context overflow."""
    return failure is not None and failure.context_overflow


def provider_failure_hint(failure: LLMFailure | None) -> str:
    detail = " ".join(failure.detail.split()) if failure else ""
    if not detail:
        return ""
    return f" Last provider error: {detail}"


def provider_recovery_hint(failure: LLMFailure | None) -> str:
    """Explain whether retrying later is likely to help."""
    detail = failure.detail.lower() if failure else ""
    if "prefill" in detail or "conversation must end with a user message" in detail:
        return (
            " This looks like a client-side transcript-shape error, not a "
            "provider outage; retrying the same input will not help."
        )
    if "provider returned incomplete response" in detail or "finish_reason=null" in detail:
        return (
            " The provider returned incomplete responses repeatedly; this may "
            "be transient, but it can also indicate malformed client input."
        )
    return ""


__all__ = [
    "MODEL_UNAVAILABLE_MESSAGE",
    "is_model_unavailable",
    "is_token_overflow",
    "provider_failure_hint",
    "provider_recovery_hint",
]
