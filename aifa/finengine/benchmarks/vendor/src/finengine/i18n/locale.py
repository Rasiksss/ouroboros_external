"""Reply locale for one request.

Three language axes live in this service and must not be conflated:

- **reply locale** — the language the assistant speaks. This module.
- **UI locale** — what the frontend renders (`web/src/i18n`). Same switcher,
  travels as `reply_locale` on the chat request.
- **document language** — `DocumentMeta.language`, the language the report is
  written in. It is called `language` everywhere in this codebase, which is
  exactly why nothing here is.

They are independent: an English-speaking user reads a Russian report and
expects the analysis in English.

The locale is resolved once per request and parked in a ContextVar, so a new
LLM surface can read it without threading a parameter through every caller in
between. `asyncio.create_task` copies the current context, so background runs
spawned by `run_orchestrator.start_run` inherit whatever the request set.
"""
from __future__ import annotations

import contextvars as _cv
from typing import Literal, get_args

Locale = Literal["ru", "en"]

DEFAULT_LOCALE: Locale = "ru"
SUPPORTED_LOCALES: tuple[Locale, ...] = get_args(Locale)

_REPLY_LOCALE: _cv.ContextVar[Locale] = _cv.ContextVar(
    "finengine_reply_locale", default=DEFAULT_LOCALE
)


def normalize(value: str | None) -> Locale:
    """Map an arbitrary client string onto a supported locale.

    Handles regional tags ("en-GB", "en_US" → "en"). Anything unsupported
    falls back to the default instead of raising: a junk locale is never a
    reason to fail a chat request.
    """
    tag = (value or "").strip().lower().replace("_", "-").split("-")[0]
    for locale in SUPPORTED_LOCALES:
        if tag == locale:
            return locale
    return DEFAULT_LOCALE


def set_reply_locale(value: str | None) -> Locale:
    """Resolve and store the reply locale for the current context."""
    locale = normalize(value)
    _REPLY_LOCALE.set(locale)
    return locale


def reply_locale() -> Locale:
    """The reply locale for the current context (default if never set)."""
    return _REPLY_LOCALE.get()
