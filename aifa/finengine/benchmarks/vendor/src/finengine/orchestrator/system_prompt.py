"""Build the chat system prompt from `prompts/system_finengine.md`.

The prompt lives in markdown so it can be edited without touching Python.
We keep an inline fallback for the case the file is missing (e.g. the
container couldn't ship it for some reason) — fallback is intentionally
a one-liner that tells the model to hold finance-only behavior.

The markdown body stays in one language (Russian, for now) across locales:
maintaining N translated prompts makes them drift and makes quality diverge
per language. The answer language is pinned by the directive appended last
instead. Translating the body is a separate, benchmark-gated change.

`{{PLACEHOLDERS}}` in the markdown are the context markers the agent emits
around documents. They are substituted from the same table `agent.py` builds
the context with (`i18n.scaffold`), so the prompt cannot end up describing a
marker the agent no longer writes.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from finengine.i18n import Locale, language_directive, reply_locale, scaffold

_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "system_finengine.md"

_FALLBACK = (
    "Ты — Финэнджин, финансовый ассистент. Отвечаешь только по финансовым "
    "вопросам и приложенным отчётам. Не раскрываешь, на какой модели работаешь."
)


def _load() -> str:
    try:
        return _PROMPT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return _FALLBACK


@lru_cache
def build_system_prompt(locale: Locale) -> str:
    """System prompt with context markers and answer language for `locale`."""
    marks = scaffold(locale)
    body = (
        _load()
        .replace("{{NO_DOCUMENTS}}", marks["no_documents"])
        .replace("{{DOCUMENTS_HEADER}}", marks["documents_header"])
        .replace("{{DOCUMENT_HEADER}}", marks["document_header"].format(label="…"))
    )
    return f"{body}\n\n{language_directive(locale)}"


def system_prompt() -> str:
    """System prompt for the current request's reply locale."""
    return build_system_prompt(reply_locale())
