"""Accumulating per-call LLM usage into a running total.

Named by both sides of the seam — the loop's nodes fold compaction usage into
``RunState.accumulated_usage``, the ouroboros LLM path folds each provider response into
the same dict — so the accumulation rule lives here, on the layer both can import.
"""

from __future__ import annotations

from typing import Any

#: Usage keys accumulated additively.
_USAGE_TOKEN_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_write_tokens",
)


def add_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
    """Accumulate usage from one LLM call into a running total."""
    for key in _USAGE_TOKEN_KEYS:
        total[key] = int(total.get(key) or 0) + int(usage.get(key) or 0)


__all__ = ["add_usage"]
