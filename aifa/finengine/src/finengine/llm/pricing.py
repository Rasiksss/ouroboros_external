"""Per-model token pricing for cost tracking.

Numbers are USD per **1M tokens** (input / output / cached input).
Source: OpenRouter pricing page as of 2026-05-25 — keep this in
sync when models change. Cached is treated as a discounted input
rate (typically ~10% of full input price).

API:
    cost_usd(usage: dict) -> float
        usage shape: {"in": int, "out": int, "cached": int, "model": str}

If a model is unknown, falls back to "openrouter/moonshotai/kimi-k2.5"
rate (current default in OpenClaw config) — overestimates rather than
silently zero, so we never under-report burn.
"""
from __future__ import annotations

from typing import TypedDict

# In $ per 1M tokens. Keep names lowercased; we strip OpenRouter prefixes
# (`openrouter/`, `anthropic/`, `moonshotai/`) on lookup.
PRICES: dict[str, dict[str, float]] = {
    "kimi-k2.5":               {"in": 0.60, "out": 2.50, "cached": 0.06},
    "kimi-k2":                 {"in": 0.30, "out": 1.20, "cached": 0.03},
    "claude-opus-4-7":         {"in": 15.00, "out": 75.00, "cached": 1.50},
    "claude-sonnet-4-6":       {"in":  3.00, "out": 15.00, "cached": 0.30},
    "claude-haiku-4-5":        {"in":  0.80, "out":  4.00, "cached": 0.08},
    "gpt-5":                   {"in":  2.50, "out": 10.00, "cached": 0.25},
    "gpt-5-mini":              {"in":  0.15, "out":  0.60, "cached": 0.015},
    "qwen-3.6-plus":           {"in":  0.50, "out":  2.00, "cached": 0.05},
    "glm-5.1":                 {"in":  0.50, "out":  2.00, "cached": 0.05},
    "deepseek-r1":             {"in":  0.55, "out":  2.19, "cached": 0.05},
    "deepseek-chat-v3-0324":   {"in":  0.20, "out":  0.77, "cached": 0.02},
    # Cloud.ru, каталог 02.09.2026: 183 / 732 ₽ за млн при 86,753 ₽/$.
    # Кэш у Cloud.ru не объявлен — ставим 10% от входа, как у остальных.
    "deepseek-v4-pro":         {"in":  2.11, "out": 8.44, "cached": 0.21},
    "deepseek-v3":             {"in":  0.20, "out":  0.77, "cached": 0.02},
}

_FALLBACK = "kimi-k2.5"


class Usage(TypedDict, total=False):
    in_: int  # mypy-only alias; we read "in"
    out: int
    cached: int
    model: str


def _canonical(model: str) -> str:
    """Trim provider prefixes so 'openrouter/moonshotai/kimi-k2.5' → 'kimi-k2.5'."""
    if not model:
        return ""
    m = model.strip().lower()
    # `deepseek-ai/` — написание Cloud.ru (02.09.2026): та же модель, что
    # `deepseek/…` у OpenRouter, но с другим префиксом и регистром.
    for prefix in ("openrouter/", "anthropic/", "moonshotai/", "openai/", "qwen/",
                   "deepseek-ai/", "deepseek/", "google/", "thudm/", "z-ai/", "z-llm/"):
        if m.startswith(prefix):
            m = m[len(prefix):]
    # Strip a known suffix like ":free", ":beta" sometimes present on OR.
    if ":" in m:
        m = m.split(":", 1)[0]
    return m


def cost_usd(usage: dict) -> float:
    """Compute USD cost for one LLM turn. Treats cached separately when
    provider reports it (sub-bills input). Never negative; clamps None→0."""
    model = _canonical(str(usage.get("model") or ""))
    rates = PRICES.get(model) or PRICES[_FALLBACK]

    tokens_in = max(0, int(usage.get("in", 0) or 0))
    tokens_out = max(0, int(usage.get("out", 0) or 0))
    tokens_cached = max(0, int(usage.get("cached", 0) or 0))

    # If `cached` is included inside `in`, subtract before billing the
    # non-cached part. (Anthropic & OpenRouter both report it this way.)
    tokens_fresh_in = max(0, tokens_in - tokens_cached)

    cost = (
        tokens_fresh_in * rates["in"]
        + tokens_cached * rates["cached"]
        + tokens_out * rates["out"]
    ) / 1_000_000
    return round(cost, 6)
