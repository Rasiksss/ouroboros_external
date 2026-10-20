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
import logging
from typing import AsyncIterator

from openai import AsyncOpenAI

from finengine.config import settings

log = logging.getLogger(__name__)


class ModelSilent(RuntimeError):
    """Модель отработала, но ответа не дала — весь бюджет ушёл на размышления.

    Отдельный тип, потому что повторять такой вызов бессмысленно: бюджет тот
    же, поведение то же, а каждая попытка стоит до 360 с. Ретрай-петля ниже
    пропускает его наружу сразу.
    """


# Уровень размышлений по умолчанию. Решение владельца 17.08.2026 — на максимум.
#
# ⚠️ ЦЕНА, ИЗМЕРЕННАЯ В ТОТ ЖЕ ДЕНЬ: токены размышлений считаются в общий
# `max_completion_tokens`. Календарь звал модель с бюджетом 1500 и не получал
# ответа вовсе — весь бюджет уходил на монолог (замер: 1500 → 4330 знаков
# размышлений и падение разбора, 4000 → 120 знаков чистого JSON). Поднимая
# уровень, СМОТРИ на бюджет вызова: тесный бюджет + высокий уровень = отказ.
# Оставшиеся тесные места: `skills/aifa/extractor.py` (1500),
# `skills/aifa_baseline/extractor.py` (2500), `llm/tools.py` (400).
_REASONING_EFFORT = "high"
_MAX_COMPLETION_TOKENS = 16000
_TEMPERATURE = 0.6
# Тяжёлая модель на большом контексте (полный анализ, пакет ~110К) физически
# считает ~200с. При 180с клиент бросал вызов ДО ответа → 3 ретрая × 180с →
# wrap-таймаут fe_analyze 600с (оба full падали). 360с — с запасом над реальным
# временем, но ниже wrap-таймаутов; истинный хэнг всё равно ограничен ими.
_TIMEOUT = 360.0
_RETRIES = 3


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


async def _close_client(client: object) -> None:
    """Close a per-call OpenAI client before its event loop is torn down.

    Most atom adapters execute this async module through ``asyncio.run``.  If
    the client's httpx transport is left to garbage collection, its finalizer
    may run after that temporary loop has closed and emit
    ``RuntimeError: Event loop is closed``.  Test doubles need not implement
    ``close``.
    """
    close = getattr(client, "close", None)
    if close is not None:
        try:
            await close()
        except Exception:  # noqa: BLE001 — cleanup must not replace the model outcome
            log.warning("OpenAI client close failed", exc_info=True)


def _is_reasoning_model(model: str) -> bool:
    """Модели, которым имеет смысл слать `reasoning` extra_body.

    ⚠️ СПИСОК ОТСТАВАЛ ОТ БОЕВОЙ МОДЕЛИ (17.08.2026). Здесь стояли только GLM и
    o1/o3, а работает шлюз на `qwen/qwen3.6-plus` — то есть ручка уровня
    размышлений НЕ ДОХОДИЛА до модели вообще, и «выкрутить ризонинг» ничего не
    меняло. Проверено вызовом: `_is_reasoning_model("qwen/qwen3.6-plus")` даёт
    False, `reasoning` в запрос не попадает.

    Замер 17.08 на задаче извлечения событий календаря (3 прогона на модель,
    effort=high, бюджет 4000):
        qwen/qwen3.6-plus          32,5 с (20,7–38,7), 1841 ток, JSON 3/3
        minimax/minimax-m3         32,1 с (6,6–74,2),   892 ток, JSON 3/3
        deepseek/deepseek-v4-flash 10,1 с (5,7–13,8),   588 ток, JSON 3/3
    Все три размышляют по факту — значит и параметр им слать осмысленно.
    """
    m = model.lower()
    return (
        "glm-5" in m or "glm-4.6" in m or "o1" in m or "o3" in m
        or "qwen3" in m or "minimax" in m or "deepseek-v4" in m or "deepseek-r1" in m
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
    Pass None to clear back to the module default."""
    if effort and effort.lower() in ("low", "medium", "high"):
        _REASONING_CTX.set(effort.lower())
    else:
        _REASONING_CTX.set(None)


def _extra_body(
    model: str,
    pin_provider: str | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    out: dict = {}
    if _is_reasoning_model(model):
        # Resolution: per-call kwarg > per-request contextvar > module default.
        # Allowed values per OpenRouter: "low" | "medium" | "high".
        eff = (reasoning_effort or _REASONING_CTX.get() or _REASONING_EFFORT).lower()
        if eff not in ("low", "medium", "high"):
            eff = _REASONING_EFFORT
        if settings.reasoning_style == "openai":
            out["reasoning_effort"] = eff
        else:
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
    omit_max_output: bool = False,
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
        omit_max_output=omit_max_output,
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
    omit_max_output: bool = False,
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
        per-metric extractor; the chat/analysis path keeps the default.
      - `pin_provider`: see `_extra_body`. Defaults to the env var.
    """
    # Route GigaChat models to the native SDK wrapper. We don't proxy them
    # through OpenRouter — Sber doesn't expose GigaChat there, and the SDK
    # has the right SSL handling for Sber's corporate CA chain.
    from finengine.llm import gigachat as _giga
    if _giga.is_gigachat_model(model):
        text, usage = await _giga.complete_with_usage(
            prompt=prompt,
            system=system,
            model=model,
            max_output=max_output or _MAX_COMPLETION_TOKENS,
            temperature=_TEMPERATURE if temperature is None else temperature,
        )
        usage_full = {"in": usage.get("in", 0), "out": usage.get("out", 0), "cached": 0, "model": model or ""}
        return text, usage_full

    # #35 cost guard: если входная точка прогона объявила бюджет (contextvar),
    # разгон по вызовам/токенам останавливается здесь — единой точке всех
    # LLM-вызовов движка. Вне прогона — no-op.
    from finengine.llm import budget as _budget  # noqa: PLC0415
    _budget.precheck_and_count()

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
                temperature=_TEMPERATURE if temperature is None else temperature,
                extra_body=_extra_body(m, pin_provider=pin_provider, reasoning_effort=reasoning_effort),
            )
            # `omit_max_output` — механика Finengine (#257): на стадии сжатия
            # контекста двухстадийного извлечения потолок длины НЕ ставится,
            # модель сама решает, сколько цитат вернуть. Наш потолок там
            # обрезал бы выжимку на середине, и вторая стадия получила бы
            # неполные свидетельства, не заметив этого.
            if not omit_max_output:
                kwargs["max_completion_tokens"] = max_output or _MAX_COMPLETION_TOKENS
            if seed is not None:
                kwargs["seed"] = seed
            if response_format:
                kwargs["response_format"] = response_format
            resp = await client.chat.completions.create(**kwargs)
            choice = resp.choices[0].message
            content = choice.content or ""
            # ⚠️ РАЗМЫШЛЕНИЯ — НЕ ОТВЕТ (11.08.2026, прогон ФосАгро).
            #
            # Здесь стояло `if not content and choice.reasoning: content =
            # choice.reasoning`. Механика отказа: модель рассуждающая, токены
            # размышлений считаются в max_completion_tokens; на бюджете 6500
            # она израсходовала его целиком на размышления и до ответа не
            # дошла — content пустой. Код молча подставил монолог вместо
            # отчёта, и дальше ВСЯ цепочка приняла его за анализ: разбор не
            # нашёл ни одного раздела, карточка вышла пустой, на экране
            # осталась подпись под единственным графиком. В сохранённом
            # диалоге это видно дословно: «Here's a thinking process:» и обрыв
            # на полуслове «I need to make sure I».
            #
            # Пустой content — это ОТКАЗ. Подстановка превращала «не получилось»
            # в «вот результат» — ровно то, что мы вычищаем из системы.
            # Размышления сохраняем в лог для разбора, но наружу не отдаём.
            finish = getattr(resp.choices[0], "finish_reason", "") or ""
            if not content:
                _think = str(getattr(choice, "reasoning", "") or "")
                log.error(
                    "модель не выдала ответ: model=%s finish_reason=%s "
                    "размышлений %d знаков, хвост: %r",
                    m, finish, len(_think), _think[-300:],
                )
                raise ModelSilent(
                    f"модель не выдала ответ (finish_reason={finish or 'нет'}; "
                    f"размышлений {len(_think)} знаков). "
                    "Похоже, бюджет вывода израсходован на размышления"
                )
            if finish == "length":
                # Ответ есть, но оборван на полуслове. Молчать нельзя: обрубок
                # выглядит как готовый отчёт и уезжает в карточку.
                log.error(
                    "ответ оборван по лимиту: model=%s max_output=%s знаков=%d",
                    m, max_output or _MAX_COMPLETION_TOKENS, len(content),
                )
            usage: dict = {"in": 0, "out": 0, "cached": 0, "model": m}
            if getattr(resp, "usage", None):
                usage["in"] = int(resp.usage.prompt_tokens or 0)
                usage["out"] = int(resp.usage.completion_tokens or 0)
                details = getattr(resp.usage, "prompt_tokens_details", None)
                if details and getattr(details, "cached_tokens", None):
                    usage["cached"] = int(details.cached_tokens)
            if content:
                _budget.add_usage(usage)
                # Auto-trace every call. Caller sets `trace_ctx` before
                # invoking (extract / chains / synthesis paths). If no
                # ctx, log_call is a no-op so this is safe for tests too.
                try:
                    from finengine.llm import traces as _traces  # noqa: PLC0415
                    _traces.log_call(usage)
                except Exception:  # noqa: BLE001
                    pass
                await _close_client(client)
                return content, usage
        except ModelSilent:
            await _close_client(client)
            raise  # повтор с тем же бюджетом даст тот же исход — не тратим 360 с
        except asyncio.CancelledError:
            await _close_client(client)
            raise
        except Exception as e:  # noqa: BLE001 — network/API, retry
            last_err = e
        await asyncio.sleep(2 * (attempt + 1))
    await _close_client(client)
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
    # #35 cost guard — та же точка учёта, что в complete_with_usage
    # (extraction-путь стримит сегменты через stream()).
    from finengine.llm import budget as _budget  # noqa: PLC0415
    _budget.precheck_and_count()

    # GigaChat path — native SDK doesn't expose true streaming, so the
    # wrapper yields the whole response as one chunk. UI sees a single
    # large delta instead of progressive rendering. Acceptable for
    # pilot integration.
    from finengine.llm import gigachat as _giga
    if _giga.is_gigachat_model(model):
        async for delta in _giga.stream(
            prompt=prompt,
            system_prompt=system_prompt,
            model=model,
            max_output=max_output or _MAX_COMPLETION_TOKENS,
            usage_sink=usage_sink,
        ):
            yield delta
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
            # OpenRouter sends a usage-only chunk at the end (no choices) when
            # stream_options.include_usage=True. Drop it into the sink so the
            # caller can compute cost.
            if not chunk.choices:
                if getattr(chunk, "usage", None):
                    _budget.add_usage({
                        "in": int(chunk.usage.prompt_tokens or 0),
                        "out": int(chunk.usage.completion_tokens or 0),
                    })
                if usage_sink is not None and getattr(chunk, "usage", None):
                    usage_sink["in"] = int(chunk.usage.prompt_tokens or 0)
                    usage_sink["out"] = int(chunk.usage.completion_tokens or 0)
                    details = getattr(chunk.usage, "prompt_tokens_details", None)
                    if details and getattr(details, "cached_tokens", None):
                        usage_sink["cached"] = int(details.cached_tokens)
                    usage_sink["model"] = m
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                yield piece
    finally:
        await _close_client(client)
