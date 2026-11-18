"""Per-model pricing table + cost calculator.

Source of truth: OpenRouter model pages (verified 2026-04-27). Update the
table when OpenRouter rotates rates. Prices are USD per 1M tokens.
"""
from __future__ import annotations


# Per-million-token pricing. Each entry: input new / input cached / output.
PRICING_USD_PER_M: dict[str, dict[str, float]] = {
    "qwen/qwen3.6-plus":     {"in": 0.325, "cached": 0.065,  "out": 1.95},
    "moonshotai/kimi-k2.5":  {"in": 0.44,  "cached": 0.088,  "out": 2.00},
    "z-ai/glm-5.1":          {"in": 1.05,  "cached": 0.21,   "out": 3.50},
}


def normalize_model_id(model_id: str) -> str:
    """Map provider snapshot ids to the pricing key we maintain locally."""
    model_id = str(model_id or "")
    if model_id.startswith("qwen/qwen3.6-plus"):
        return "qwen/qwen3.6-plus"
    if model_id.startswith("moonshotai/kimi-k2.5"):
        return "moonshotai/kimi-k2.5"
    if model_id.startswith("z-ai/glm-5.1"):
        return "z-ai/glm-5.1"
    return model_id


def cost_usd(model_id: str, tokens_in: int, tokens_out: int, cached_tokens: int = 0) -> float:
    """Compute the USD cost of one LLM call.

    `cached_tokens` is the subset of `tokens_in` that hit the provider's
    prompt cache (charged at the cached rate). `tokens_in` here is the FULL
    input — we subtract cached internally so the caller doesn't double-pay.
    """
    p = PRICING_USD_PER_M.get(normalize_model_id(model_id))
    if not p:
        return 0.0
    new_in = max(0, int(tokens_in) - int(cached_tokens or 0))
    return (
        new_in * p["in"]
        + int(cached_tokens or 0) * p["cached"]
        + int(tokens_out) * p["out"]
    ) / 1_000_000


def aggregate_usage_to_diag(usage_sum: dict, diag: dict, model_id: str | None) -> None:
    """Normalize a running usage dict and stamp cost on `diag`. Called from
    each extractor right before `done` event."""
    diag["tokens_in"] = int(usage_sum.get("in", 0))
    diag["tokens_out"] = int(usage_sum.get("out", 0))
    diag["tokens_cached"] = int(usage_sum.get("cached", 0))
    diag["llm_calls"] = int(usage_sum.get("calls", 0))
    usage_model = str(usage_sum.get("model") or model_id or "")
    diag["cost_usd"] = round(
        cost_usd(
            usage_model,
            diag["tokens_in"],
            diag["tokens_out"],
            diag["tokens_cached"],
        ),
        4,
    )
