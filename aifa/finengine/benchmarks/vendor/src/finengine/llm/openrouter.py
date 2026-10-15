"""OpenRouter client for chat and single-shot metric extraction.

Replaces Claude Agent SDK in the production path. Drop-in async API:
  - `stream(prompt, system_prompt, model=None)`   -> AsyncIterator[str]   (chat)
  - `complete(prompt, model=None, system=None)`   -> str                  (extraction)

GLM-5.1 is a reasoning model — passes `max_completion_tokens=16000`,
`temperature=0.6`, `reasoning.effort="medium"` which are the validated
extraction settings (see tests/validator.py).
"""
from __future__ import annotations

import asyncio
import contextvars as _cv
import os
import weakref
from typing import AsyncIterator

from openai import AsyncOpenAI

from finengine.config import settings

_REASONING_EFFORT = "medium"
_MAX_COMPLETION_TOKENS = 16000
_TEMPERATURE = 0.6
_TIMEOUT = float(os.getenv("FINENGINE_OPENROUTER_TIMEOUT", "180"))
_RETRIES = 3


# One gate per event loop, not a single module-level semaphore: the API server
# runs on uvicorn's loop, while background document parsing calls asyncio.run()
# inside a threadpool and so brings a loop of its own. asyncio primitives are
# bound to a loop and are not thread-safe, so sharing one across both is wrong.
_IN_FLIGHT: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _gate() -> asyncio.Semaphore:
    """Cap concurrent provider requests — replaces the LLM proxy's queue."""
    loop = asyncio.get_running_loop()
    sem = _IN_FLIGHT.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(max(1, settings.llm_max_in_flight))
        _IN_FLIGHT[loop] = sem
    return sem


def _client() -> AsyncOpenAI:
    if not settings.openrouter_api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Provide it in the environment."
        )
    return AsyncOpenAI(
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key,
        timeout=_TIMEOUT,
    )


# Per-request reasoning-effort override. The API layer sets this from the
# `?reasoning_effort=` query param at the start of each request — asyncio
# `contextvars` are task-local, so concurrent requests don't bleed into
# each other. Falling back to module default `_REASONING_EFFORT` when unset.
_REASONING_CTX: _cv.ContextVar[str | None] = _cv.ContextVar(
    "finengine_reasoning_effort", default=None
)


def set_reasoning_effort(effort: str | None) -> None:
    """Set the reasoning-effort override for the current asyncio task.

    Pass ``None`` to clear back to the module default. Pass ``"none"`` to omit
    the OpenRouter reasoning block for deterministic benchmark comparisons.
    """
    normalized = (effort or "").strip().lower()
    if normalized in ("low", "medium", "high", "none"):
        _REASONING_CTX.set(normalized)
    else:
        _REASONING_CTX.set(None)


def _extra_body(
    model: str,
    pin_provider: str | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    """Build OpenRouter `extra_body` payload.

    History note: earlier this code maintained a `_is_reasoning_model`
    whitelist and only sent the `reasoning` parameter for known reasoning
    models. That was wrong on two counts:
      1. Empirical test (2026-05-03) showed Qwen 3.6 Plus, MiniMax M2 and
         DeepSeek V3.x ALSO produce reasoning_tokens — they're just hidden
         from OpenRouter's catalog. The whitelist missed all of them.
      2. OpenRouter accepts `reasoning: {effort}` for ANY model — providers
         that don't support reasoning silently ignore it. So passing it
         universally is safe and correct.

    Now: always send the effort if one is set. Resolution order remains
    per-call kwarg > per-request contextvar > module default ("medium").
    """
    out: dict = {}
    eff = (reasoning_effort or _REASONING_CTX.get() or _REASONING_EFFORT).lower()
    if eff == "none":
        eff = ""
    if eff in ("low", "medium", "high"):
        out["reasoning"] = {"effort": eff}
    # OpenRouter routes the same model across multiple providers; different
    # backends introduce micro-variance even at temp=0 (we observed
    # 18-of-103 metrics drifting between two duplicate AIFA runs). Pinning
    # the request to one provider removes that source.
    #
    # Caveat: a provider may not host every model. Forcing "Alibaba" on
    # Kimi K2.5 returns
    #   404 No allowed providers are available for the selected model.
    # So the pin is model-aware: each known provider maps to model-id
    # patterns it actually serves; the pin is dropped when there's no
    # match (OpenRouter falls back to default routing).
    #
    # Env var `FINENGINE_OPENROUTER_PROVIDER` sets the default;
    # per-call argument wins.
    provider = (pin_provider or settings.openrouter_provider or "").strip()
    if provider:
        m = model.lower()
        # provider name → list of model-id substrings it can serve
        _PROVIDER_MODEL_MATCH = {
            "alibaba": ("qwen",),
            "moonshot": ("kimi",),
            "moonshotai": ("kimi",),
            "z.ai": ("glm",),
            "zhipuai": ("glm",),
            "anthropic": ("claude",),
        }
        patterns = _PROVIDER_MODEL_MATCH.get(provider.lower())
        # Unknown provider → trust the operator and let OpenRouter validate.
        if patterns is None or any(p in m for p in patterns):
            out["provider"] = {"only": [provider]}
    return out


def _resolve_model(model: str | None) -> str:
    return model or settings.openrouter_model


async def complete(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    retries: int = _RETRIES,
    response_format: dict | None = None,
    max_output: int | None = None,
    *,
    seed: int | None = None,
    temperature: float | None = None,
    pin_provider: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """Backwards-compatible wrapper that returns just the text.

    Most call-sites only care about the response. New callers that also
    want token usage (for cost accounting) should use `complete_with_usage`.
    Determinism knobs (seed/temperature/pin_provider) pass through.
    """
    text, _ = await complete_with_usage(
        prompt=prompt,
        model=model,
        system=system,
        retries=retries,
        response_format=response_format,
        max_output=max_output,
        seed=seed,
        temperature=temperature,
        pin_provider=pin_provider,
        reasoning_effort=reasoning_effort,
    )
    return text


async def complete_with_usage(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    retries: int = _RETRIES,
    response_format: dict | None = None,
    max_output: int | None = None,
    *,
    seed: int | None = None,
    temperature: float | None = None,
    pin_provider: str | None = None,
    reasoning_effort: str | None = None,
) -> tuple[str, dict]:
    """Like `complete`, but also returns the provider's token usage dict:
        { "in": int, "out": int, "cached": int, "model": str }
    Empty dict on failure. The caller feeds this into pricing.cost_usd.

    Determinism knobs:
      - `seed`: forwarded to the OpenAI API. Most providers honour it as a
        hint, not a hard guarantee; combined with temperature=0 and a pinned
        provider you get reproducible outputs in practice. AIFA per-metric
        passes seed=hash(doc_id+metric_enum).
      - `temperature`: overrides the module default (0.6). Pass 0 for the
        per-metric extractor; reasoner chains keep the default.
      - `pin_provider`: see `_extra_body`. Defaults to the env var.
    """
    # Universal trace — every LLM call writes one line to llm_traces/.
    # Reads run_id/kind/doc_id/user_id/arch from contextvars set by API
    # endpoints, plus per-task `extra` for caller-specific tags.
    from finengine.llm.tracer import write_trace as _write_trace, time_now as _time_now
    _t_start = _time_now()

    # Route GigaChat models to the native SDK wrapper. We don't proxy them
    # through OpenRouter — Sber doesn't expose GigaChat there, and the SDK
    # has the right SSL handling for Sber's corporate CA chain.
    from finengine.llm import gigachat as _giga
    if _giga.is_gigachat_model(model):
        try:
            text, usage = await _giga.complete_with_usage(
                prompt=prompt,
                system=system,
                model=model,
                max_output=max_output or _MAX_COMPLETION_TOKENS,
                temperature=_TEMPERATURE if temperature is None else temperature,
            )
        except Exception as e:
            _write_trace(
                model=model or "", system_prompt=system, prompt=prompt, response="",
                latency_s=_time_now() - _t_start, error=str(e)[:500],
            )
            raise
        usage_full = {"in": usage.get("in", 0), "out": usage.get("out", 0), "cached": 0, "model": model or ""}
        _write_trace(
            model=model or "", system_prompt=system, prompt=prompt, response=text,
            tokens_in=usage_full["in"], tokens_out=usage_full["out"], tokens_cached=0,
            latency_s=_time_now() - _t_start,
        )
        return text, usage_full

    m = _resolve_model(model)
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    client = _client()
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            kwargs: dict = dict(
                model=m,
                messages=messages,
                max_completion_tokens=max_output or _MAX_COMPLETION_TOKENS,
                temperature=_TEMPERATURE if temperature is None else temperature,
                extra_body=_extra_body(m, pin_provider=pin_provider, reasoning_effort=reasoning_effort),
            )
            if seed is not None:
                kwargs["seed"] = seed
            if response_format:
                kwargs["response_format"] = response_format
            # Acquired per attempt, so a task sleeping between retries below
            # does not sit on a slot it is not using.
            async with _gate():
                resp = await client.chat.completions.create(**kwargs)
            choice = resp.choices[0].message
            content = choice.content or ""
            if not content and hasattr(choice, "reasoning") and choice.reasoning:
                content = choice.reasoning
            usage: dict = {"in": 0, "out": 0, "cached": 0, "model": m}
            if getattr(resp, "usage", None):
                usage["in"] = int(resp.usage.prompt_tokens or 0)
                usage["out"] = int(resp.usage.completion_tokens or 0)
                details = getattr(resp.usage, "prompt_tokens_details", None)
                if details and getattr(details, "cached_tokens", None):
                    usage["cached"] = int(details.cached_tokens)
            if content:
                _write_trace(
                    model=m, system_prompt=system, prompt=prompt, response=content,
                    tokens_in=usage["in"], tokens_out=usage["out"], tokens_cached=usage.get("cached", 0),
                    latency_s=_time_now() - _t_start,
                )
                return content, usage
        except Exception as e:  # noqa: BLE001 — network/API, retry
            last_err = e
        await asyncio.sleep(2 * (attempt + 1))
    # All retries exhausted.
    _write_trace(
        model=m, system_prompt=system, prompt=prompt, response="",
        latency_s=_time_now() - _t_start,
        error=f"all {retries} retries exhausted: {last_err}" if last_err else "empty response",
    )
    if last_err:
        raise last_err
    return "", {"in": 0, "out": 0, "cached": 0, "model": m}


async def stream(
    prompt: str,
    system_prompt: str | None = None,
    model: str | None = None,
    response_format: dict | None = None,
    max_output: int | None = None,
    usage_sink: dict | None = None,
    reasoning_effort: str | None = None,
) -> AsyncIterator[str]:
    """Streaming chat completion. Yields text deltas as they arrive.

    `usage_sink` (optional dict) is filled in-place with the final
    {"in", "out", "cached"} once the stream completes — the OpenAI API
    sends usage in the last chunk when stream_options.include_usage=True.
    """
    # Universal trace — buffer the whole response as deltas arrive, write
    # one trace line on completion (success or error). Streaming traces
    # capture the FULL response after assembly, not delta-by-delta.
    from finengine.llm.tracer import write_trace as _write_trace, time_now as _time_now
    _t_start = _time_now()
    _captured: list[str] = []
    _captured_usage = {"in": 0, "out": 0, "cached": 0}

    # GigaChat path — native SDK doesn't expose true streaming, so the
    # wrapper yields the whole response as one chunk. UI sees a single
    # large delta instead of progressive rendering. Acceptable for
    # pilot integration.
    from finengine.llm import gigachat as _giga
    if _giga.is_gigachat_model(model):
        try:
            async for delta in _giga.stream(
                prompt=prompt,
                system_prompt=system_prompt,
                model=model,
                max_output=max_output or _MAX_COMPLETION_TOKENS,
                usage_sink=usage_sink,
            ):
                _captured.append(delta)
                yield delta
        except Exception as e:
            _write_trace(
                model=model or "", system_prompt=system_prompt, prompt=prompt,
                response="".join(_captured), latency_s=_time_now() - _t_start,
                error=str(e)[:500],
            )
            raise
        if usage_sink:
            _captured_usage = {k: int(usage_sink.get(k, 0) or 0) for k in ("in", "out", "cached")}
        _write_trace(
            model=model or "", system_prompt=system_prompt, prompt=prompt,
            response="".join(_captured),
            tokens_in=_captured_usage["in"], tokens_out=_captured_usage["out"],
            tokens_cached=_captured_usage["cached"],
            latency_s=_time_now() - _t_start,
        )
        return

    m = _resolve_model(model)
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    client = _client()
    # Reasoning models sometimes buffer content until reasoning is done — stream
    # still works, deltas just come in bursts. That is OK for UX.
    kwargs: dict = dict(
        model=m,
        messages=messages,
        max_completion_tokens=max_output or _MAX_COMPLETION_TOKENS,
        temperature=_TEMPERATURE,
        extra_body=_extra_body(m, reasoning_effort=reasoning_effort),
        stream=True,
        stream_options={"include_usage": True},
    )
    if response_format:
        kwargs["response_format"] = response_format
    try:
        stream_ = await client.chat.completions.create(**kwargs)
        async for chunk in stream_:
            usage = getattr(chunk, "usage", None)
            if usage:
                if isinstance(usage, dict):
                    _captured_usage["in"] = int(usage.get("prompt_tokens") or 0)
                    _captured_usage["out"] = int(usage.get("completion_tokens") or 0)
                    details = usage.get("prompt_tokens_details") or {}
                    cached = details.get("cached_tokens") if isinstance(details, dict) else 0
                else:
                    _captured_usage["in"] = int(getattr(usage, "prompt_tokens", 0) or 0)
                    _captured_usage["out"] = int(getattr(usage, "completion_tokens", 0) or 0)
                    details = getattr(usage, "prompt_tokens_details", None)
                    cached = getattr(details, "cached_tokens", 0) if details else 0
                if cached:
                    _captured_usage["cached"] = int(cached)
                if usage_sink is not None:
                    usage_sink["in"] = _captured_usage["in"]
                    usage_sink["out"] = _captured_usage["out"]
                    usage_sink["cached"] = _captured_usage["cached"]
                    usage_sink["model"] = m
            # OpenRouter may send a usage-only chunk at the end when
            # stream_options.include_usage=True. It has no text delta to yield.
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                _captured.append(piece)
                yield piece
    except Exception as e:
        _write_trace(
            model=m, system_prompt=system_prompt, prompt=prompt,
            response="".join(_captured), latency_s=_time_now() - _t_start,
            error=str(e)[:500],
        )
        raise
    _write_trace(
        model=m, system_prompt=system_prompt, prompt=prompt,
        response="".join(_captured),
        tokens_in=_captured_usage["in"], tokens_out=_captured_usage["out"],
        tokens_cached=_captured_usage["cached"],
        latency_s=_time_now() - _t_start,
    )
