"""The commands the runtime owns, shared by the addressing gate, the orchestrator and the gateway."""

from __future__ import annotations

import re

RUNTIME_SLASH_COMMANDS = frozenset({"/status", "/restart", "/stop"})

CLEAN_HISTORY_COMMANDS = frozenset(
    {
        "/clean",
        "clean",
        "очисти",
        "очисти историю",
        "очисти чат",
        "чисти",
        "чисти историю",
        "чисти чат",
        "почисти",
        "почисти историю",
        "почисти чат",
        "почтисти историю",
    }
)


def _normalize_clean_history_command(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    return normalized.strip(" \t\r\n.!?,;:")


def is_clean_history_command(text: str) -> bool:
    return _normalize_clean_history_command(text) in CLEAN_HISTORY_COMMANDS


def runtime_slash_command(text: str) -> str:
    """The owned slash command this text invokes, or an empty string."""
    lowered = str(text or "").strip().lower()
    for cmd in RUNTIME_SLASH_COMMANDS:
        if lowered == cmd or (cmd != "/stop" and lowered.startswith(cmd)):
            return cmd
    return ""


def is_runtime_command(text: str) -> bool:
    return bool(runtime_slash_command(text)) or is_clean_history_command(text)


__all__ = [
    "CLEAN_HISTORY_COMMANDS",
    "RUNTIME_SLASH_COMMANDS",
    "is_clean_history_command",
    "is_runtime_command",
    "runtime_slash_command",
]
