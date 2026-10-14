"""GigaChat native SDK wrapper.

Mirrors the public API of `openrouter.py` (`complete`, `complete_with_usage`,
`stream`) so the rest of the codebase can target one interface. Uses the
official `gigachat` Python SDK with base64 `client_id:secret` credentials —
the same approach Расула уses in `document_extractor_idp_ocr_Расула/giga.py`.

Important caveats vs OpenRouter:

- `verify_ssl_certs=False` — Сбер's GigaChat endpoint sits behind their
  corporate certificate authority. Public CAs don't recognize it, so SSL
  verification has to be disabled to actually establish the TLS handshake.
  Yes, this is what Сбер ships in their own SDK examples.

- The SDK is sync. We wrap each call in `asyncio.to_thread(...)` so it
  doesn't block the event loop during the API roundtrip.

- The SDK's chat method is single-shot. We don't have token-streaming
  for GigaChat (yet); the `stream(...)` async-iterator yields the whole
  response as a single delta. UI sees ~one chunk per call.

  TODO: switch to GigaChat's streaming API once the SDK exposes it
  cleanly. Until then, accept the latency cost — total wall-clock isn't
  much different, just no progressive rendering.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from finengine.config import settings

logger = logging.getLogger(__name__)


# Models GigaChat exposes via the SDK. The SDK accepts these strings as
# `model=...`. Caller passes a model id; if it matches one of these (or
# starts with `GigaChat`), we route to this wrapper instead of OpenRouter.
KNOWN_MODELS: set[str] = {
    "GigaChat",
    "GigaChat-Pro",
    "GigaChat-Max",
    "GigaChat-Lite",
    "GigaChat-2-Max",
    "GigaChat-2-Pro",
    "GigaChat-2",
}

# Defaults — tuned similarly to GigaChat docs.
_DEFAULT_MODEL = "GigaChat-Max"
_TIMEOUT = 180.0


def is_gigachat_model(model: str | None) -> bool:
    """Cheap check: does this model id route to GigaChat?"""
    if not model:
        return False
    if model in KNOWN_MODELS:
        return True
    return model.lower().startswith("gigachat")


def _credentials() -> str:
    """Read base64-encoded `client_id:secret` from settings. Raises if the
    env-var isn't set so the caller fails fast with a clear message
    instead of a deep SDK auth error."""
    cred = (settings.gigachat_credentials or "").strip()
    if not cred:
        raise RuntimeError(
            "GIGACHAT_CREDENTIALS is not set. Add the base64 client_id:secret "
            "string to your .env (same value as in AIFA's giga.py)."
        )
    return cred


def _ensure_sdk():
    """Import the gigachat SDK lazily — keeps Finengine importable on
    machines where the GigaChat dep isn't installed yet."""
    try:
        import gigachat  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "gigachat package not installed. Add `gigachat>=0.1.39` to "
            "pyproject.toml and re-run `pip install -e .`."
        ) from e


def _resolve_model(model: str | None) -> str:
    if model and model in KNOWN_MODELS:
        return model
    if model and model.lower().startswith("gigachat"):
        # Trust the caller — let the SDK raise if it's an unknown alias.
        return model
    return settings.gigachat_model or _DEFAULT_MODEL


async def complete(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    max_output: int = 4000,
    temperature: float = 0.6,
) -> str:
    """One-shot completion. Returns the full response string."""
    text, _usage = await complete_with_usage(
        prompt=prompt,
        system=system,
        model=model,
        max_output=max_output,
        temperature=temperature,
    )
    return text


async def complete_with_usage(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    max_output: int = 4000,
    temperature: float = 0.6,
    response_format: dict | None = None,
    seed: int | None = None,
    pin_provider: str | None = None,  # noqa: ARG001 — accepted for API parity
    usage_sink: dict | None = None,
) -> tuple[str, dict]:
    """One-shot completion with token usage. Mirrors openrouter helper.

    `response_format`, `seed`, `pin_provider` accepted but currently
    ignored — GigaChat SDK doesn't expose them as cleanly as OpenAI does.
    `seed` would map to `RandomSeed`, but it's a soft hint, not a hard
    determinism guarantee.
    """
    _ensure_sdk()
    # SDK is sync, run in thread pool.
    return await asyncio.to_thread(
        _sync_complete,
        prompt=prompt,
        system=system,
        model=_resolve_model(model),
        max_output=max_output,
        temperature=temperature,
        usage_sink=usage_sink,
    )


def _sync_complete(
    *,
    prompt: str,
    system: str | None,
    model: str,
    max_output: int,
    temperature: float,
    usage_sink: dict | None,
) -> tuple[str, dict]:
    """Sync GigaChat call — wrapped by `asyncio.to_thread`."""
    from gigachat import GigaChat
    from gigachat.models import Chat, Messages, MessagesRole

    messages: list[Messages] = []
    if system:
        messages.append(Messages(role=MessagesRole.SYSTEM, content=system))
    messages.append(Messages(role=MessagesRole.USER, content=prompt))

    chat = Chat(
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_output,
    )

    with GigaChat(
        credentials=_credentials(),
        model=model,
        scope=settings.gigachat_scope,
        verify_ssl_certs=False,
        timeout=_TIMEOUT,
    ) as giga:
        response = giga.chat(chat)

    # SDK response shape: response.choices[0].message.content
    text = ""
    try:
        text = response.choices[0].message.content or ""
    except (AttributeError, IndexError):
        logger.warning("Unexpected GigaChat response shape: %r", response)

    # Usage object: response.usage = Usage(prompt_tokens, completion_tokens,
    # total_tokens). Map to our usage-sink shape (`in`/`out`/`cached`).
    usage = {"in": 0, "out": 0, "cached": 0}
    try:
        u = getattr(response, "usage", None)
        if u is not None:
            usage["in"] = int(getattr(u, "prompt_tokens", 0) or 0)
            usage["out"] = int(getattr(u, "completion_tokens", 0) or 0)
    except Exception:
        pass

    if usage_sink is not None:
        usage_sink.setdefault("in", 0)
        usage_sink.setdefault("out", 0)
        usage_sink.setdefault("cached", 0)
        usage_sink["in"] += usage["in"]
        usage_sink["out"] += usage["out"]

    return text, usage


async def stream(
    prompt: str,
    *,
    system_prompt: str | None = None,
    model: str | None = None,
    max_output: int = 4000,
    temperature: float = 0.6,
    response_format: dict | None = None,  # noqa: ARG001
    usage_sink: dict | None = None,
) -> AsyncIterator[str]:
    """Streaming interface — async iterator yielding response deltas.

    GigaChat SDK doesn't expose token-by-token streaming through a clean
    public API, so we yield the whole response as one chunk after the
    completion lands. UI sees a single big delta instead of progressive
    rendering — acceptable for pilot integration.
    """
    text, _usage = await complete_with_usage(
        prompt=prompt,
        system=system_prompt,
        model=model,
        max_output=max_output,
        temperature=temperature,
        usage_sink=usage_sink,
    )
    if text:
        yield text
