"""User-facing strings the backend renders itself.

Deterministic text — statuses, errors, disclaimers — must never be produced by
the model: it has to be exact, instant and free. Where the frontend can render
a string it does (backend sends a code, see `chat.status.*` in
`web/src/i18n`); this catalog covers what the backend has to put into the
answer stream itself.

Adding a locale means adding one entry per string here and one dictionary on
the frontend — no prompt or endpoint changes.
"""
from __future__ import annotations

from finengine.i18n.locale import DEFAULT_LOCALE, Locale, reply_locale

_MESSAGES: dict[str, dict[Locale, str]] = {
    "chat.pdfTimeout": {
        "ru": (
            "⚠️ PDF не удалось обработать за 5 минут — "
            "попробуйте загрузить документ ещё раз."
        ),
        "en": (
            "⚠️ The PDF could not be processed within 5 minutes — "
            "please upload the document again."
        ),
    },
    "chat.tooManyDocs": {
        "ru": (
            "⚠️ Слишком много документов выбрано (~{tokens}K токенов). "
            "Снимите часть отчётов из контекста или задайте вопрос по одному документу."
        ),
        "en": (
            "⚠️ Too many documents selected (~{tokens}K tokens). "
            "Remove some reports from the context, or ask about a single document."
        ),
    },
    "chat.agentError": {
        "ru": "Ошибка агента: {error}",
        "en": "Agent error: {error}",
    },
    "chain.stepFailed": {
        "ru": "_(шаг не удалось выполнить: {error})_",
        "en": "_(this step could not be completed: {error})_",
    },
    "chain.summaryFailed": {
        "ru": "_(executive summary не сгенерирован: {error})_",
        "en": "_(the executive summary was not generated: {error})_",
    },
    "aifa.unsupportedDocument": {
        "ru": (
            "AIFA ещё не умеет работать с такой отчётностью. "
            "Пожалуйста, выберите другой режим."
        ),
        "en": (
            "AIFA cannot handle this kind of report yet. "
            "Please choose a different mode."
        ),
    },
    "chat.pilotDisclaimer": {
        "ru": (
            "> ⚠️ **Пилотный режим.** Автоматическая верификация ответов "
            "настраивается — пожалуйста, критически перепроверяйте "
            "ключевые показатели и выводы перед использованием.\n\n"
        ),
        "en": (
            "> ⚠️ **Pilot mode.** Automatic answer verification is still being "
            "set up — please double-check key figures and conclusions before "
            "relying on them.\n\n"
        ),
    },
}


def t(code: str, locale: Locale | None = None, **params: object) -> str:
    """Render a catalog string in the current (or given) reply locale.

    Missing translation falls back to the default locale; missing entry
    returns the code itself. Neither is worth raising over mid-stream.
    """
    entry = _MESSAGES.get(code)
    if entry is None:
        return code
    template = entry.get(locale or reply_locale()) or entry[DEFAULT_LOCALE]
    return template.format(**params) if params else template
