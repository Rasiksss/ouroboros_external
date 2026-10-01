"""Pure, dependency-free value helpers used by the loop nodes.

Pure value logic with no external effect, owned here because ``new_core/loop`` is the
loop's owner and nothing else calls it. Helpers the ouroboros side calls too are not in
here: transcript-shape rules live in ``new_core.loop.transcript_merge`` and usage
accumulation in ``new_core.llm.usage``, each a single implementation both layers import.
"""

from __future__ import annotations

from typing import Any


def is_token_overflow(accumulated_usage: dict[str, Any]) -> bool:
    """Whether the last LLM error looks like a context/token overflow."""
    detail = str(accumulated_usage.get("_last_llm_error") or "").lower()
    has_token_keyword = "token" in detail and ("exceed" in detail or "limit" in detail or "count" in detail)
    has_context_keyword = "context" in detail and ("length" in detail or "too long" in detail or "exceed" in detail)
    return has_token_keyword or has_context_keyword


def provider_failure_hint(accumulated_usage: dict[str, Any]) -> str:
    detail = " ".join(str(accumulated_usage.get("_last_llm_error") or "").split()).strip()
    if not detail:
        return ""
    return f" Last provider error: {detail}"


def provider_recovery_hint(accumulated_usage: dict[str, Any]) -> str:
    """Explain whether retrying later is likely to help."""
    detail = str(accumulated_usage.get("_last_llm_error") or "").lower()
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
    "is_token_overflow",
    "provider_failure_hint",
    "provider_recovery_hint",
]
