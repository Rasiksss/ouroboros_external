"""Detect unexpected foreign scripts in a final model response."""
# ruff: noqa: RUF001

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScriptSpec:
    name: str
    ranges: tuple[tuple[int, int], ...]
    request_markers: tuple[str, ...]


@dataclass(frozen=True)
class LanguageViolation:
    spans: list[str]
    scripts: list[str]


_SCRIPTS = (
    ScriptSpec("Hebrew", ((0x0590, 0x05FF),), ("иврит", "hebrew")),
    ScriptSpec(
        "Arabic",
        ((0x0600, 0x06FF), (0x0750, 0x077F)),
        ("арабск", "arabic"),
    ),
    ScriptSpec("Devanagari", ((0x0900, 0x097F),), ("хинди", "деванагари", "hindi")),
    ScriptSpec("Thai", ((0x0E00, 0x0E7F),), ("тайск", "thai")),
    ScriptSpec("Hiragana", ((0x3040, 0x309F),), ("японск", "japanese")),
    ScriptSpec("Katakana", ((0x30A0, 0x30FF),), ("японск", "japanese")),
    ScriptSpec(
        "CJK",
        ((0x3400, 0x4DBF), (0x4E00, 0x9FFF)),
        ("китайск", "японск", "chinese", "japanese"),
    ),
    ScriptSpec("Hangul", ((0xAC00, 0xD7AF),), ("корейск", "korean")),
)

_REPAIR_SYSTEM_PROMPT = (
    "Ты редактор финального ответа ассистента. Удали только случайные символы и слова "
    "на посторонней письменности. Не меняй смысл, факты, язык и формат ответа. "
    "Не добавляй пояснений."
)


def _script_for_char(char: str) -> ScriptSpec | None:
    codepoint = ord(char)
    for script in _SCRIPTS:
        if any(start <= codepoint <= end for start, end in script.ranges):
            return script
    return None


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
        return "\n".join(parts)
    return str(content or "")


def _foreign_spans(text: str) -> list[tuple[str, ScriptSpec]]:
    spans: list[tuple[str, ScriptSpec]] = []
    current_chars: list[str] = []
    current_script: ScriptSpec | None = None
    for char in str(text or ""):
        script = _script_for_char(char)
        if script is None or script != current_script:
            if current_chars and current_script is not None:
                spans.append(("".join(current_chars), current_script))
            current_chars = []
            current_script = script
        if script is not None:
            current_chars.append(char)
    if current_chars and current_script is not None:
        spans.append(("".join(current_chars), current_script))
    return spans


def _evidence(messages: list[dict[str, Any]]) -> tuple[str, str]:
    evidence_parts: list[str] = []
    user_parts: list[str] = []
    for message in messages:
        role = str(message.get("role") or "")
        text = _text_content(message.get("content"))
        if role in {"user", "tool"}:
            evidence_parts.append(text)
        if role == "user":
            user_parts.append(text)
    return "\n".join(evidence_parts), "\n".join(user_parts).casefold().replace("ё", "е")


def find_language_violation(
    content: str | None,
    messages: list[dict[str, Any]] | None,
) -> LanguageViolation | None:
    spans = _foreign_spans(str(content or ""))
    if not spans:
        return None
    evidence, user_text = _evidence(list(messages or []))
    unexpected: list[str] = []
    scripts: list[str] = []
    for span, script in spans:
        explicitly_requested = any(marker in user_text for marker in script.request_markers)
        if span in evidence or explicitly_requested:
            continue
        unexpected.append(span)
        if script.name not in scripts:
            scripts.append(script.name)
    return LanguageViolation(unexpected, scripts) if unexpected else None


def build_language_repair_messages(content: str | None) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _REPAIR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Отредактируй финальный ответ ниже. Удали только случайные фрагменты "
                "на посторонней письменности. Не меняй смысл.\n\n" + str(content or "")
            ),
        },
    ]


__all__ = [
    "LanguageViolation",
    "build_language_repair_messages",
    "find_language_violation",
]
