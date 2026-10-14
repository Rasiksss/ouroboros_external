"""Model-facing text: the reply-language directive and the prompt scaffolding.

Deliberately separate from `messages.py`. That catalog is product copy shown
to a user; this one is model input, and rewording it is a prompt-engineering
change with quality consequences, not a translation task.

The scaffolding headers are mirrored on the frontend (`web/src/i18n/prompt.ts`),
which wraps the same markers around the user's question before sending it.
Keep the two in step: a Russian header from one side and an English header
from the other inside a single prompt is the mixed-context case this whole
mechanism exists to avoid.
"""
from __future__ import annotations

from finengine.i18n.locale import Locale, reply_locale

_DIRECTIVES: dict[Locale, str] = {
    "ru": (
        "## Язык ответа\n\n"
        "Пиши ответ по-русски — независимо от языка приложенных документов и "
        "названий показателей в контексте. Формулировки, приведённые выше в "
        "кавычках, задают смысл, а не дословный текст: передавай их своими "
        "словами на языке ответа. Цитаты из отчёта приводи в оригинале. Если "
        "пользователь прямо просит ответить на другом языке — выполни просьбу."
    ),
    "en": (
        "## Reply language\n\n"
        "Write the answer in English — regardless of the language of the "
        "instructions above, of the attached documents, or of the metric names in "
        "the context. The phrasings quoted above define meaning, not wording: "
        "express them in English. Quote report lines in their original language, "
        "adding an English translation when the quote carries the answer. If the "
        "user explicitly asks for another language, follow that request."
    ),
}

_SCAFFOLD: dict[Locale, dict[str, str]] = {
    "ru": {
        "history_header": "## История диалога:",
        "documents_header": "## Документы (полный текст):",
        "document_header": "## Документ: {label}",
        "document_partial_header": "## Документ: {label} (только релевантные фрагменты)",
        "no_documents": "## Документы: не загружены.",
        "question_header": "## Вопрос пользователя:",
        "instructions": (
            "Отвечай строго на основе предоставленного контекста. "
            "Указывай источник [документ] для каждого факта. "
            "Для математических расчётов используй инструмент Bash (Python)."
        ),
        "role_user": "Пользователь",
        "role_assistant": "Ассистент",
        "step": "Шаг",
    },
    "en": {
        "history_header": "## Conversation history:",
        "documents_header": "## Documents (full text):",
        "document_header": "## Document: {label}",
        "document_partial_header": "## Document: {label} (relevant excerpts only)",
        "no_documents": "## Documents: none attached.",
        "question_header": "## User question:",
        "instructions": (
            "Answer strictly from the provided context. "
            "Cite the source [document] for every fact. "
            "Use the Bash tool (Python) for arithmetic."
        ),
        "role_user": "User",
        "role_assistant": "Assistant",
        "step": "Step",
    },
}


_LANGUAGE_NAMES: dict[Locale, str] = {"ru": "Russian", "en": "English"}


def language_name(locale: Locale | None = None) -> str:
    """English name of the reply language, for embedding in a prompt rule.

    For prompts that need to name the language inline rather than append the
    full directive — a JSON-returning extractor, say, where a block of prose
    instructions would compete with "return only the JSON object".
    """
    return _LANGUAGE_NAMES[locale or reply_locale()]


def language_directive(locale: Locale | None = None) -> str:
    """The instruction that pins the answer language.

    Goes last in the system prompt: trailing instructions carry the most
    weight, and being written in the target language it doubles as an
    exemplar. It also has to explicitly outrank the examples above it —
    a prompt that quotes sample phrasing pulls the answer toward the language
    of that phrasing no matter what a directive elsewhere says.
    """
    return _DIRECTIVES[locale or reply_locale()]


def scaffold(locale: Locale | None = None) -> dict[str, str]:
    """Prompt section headers and history role labels for the given locale."""
    return _SCAFFOLD[locale or reply_locale()]
