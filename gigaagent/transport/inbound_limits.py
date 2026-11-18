"""Inbound text limits shared by external transports."""

from __future__ import annotations


def oversized_message_refusal(text: str, limit: int) -> str:
    """The refusal for a message longer than ``limit`` characters, or "" when it is admitted."""
    if len(text) <= limit:
        return ""
    return f"Сообщение слишком длинное. Максимальная длина — {limit} символов. Сократите сообщение и отправьте снова."


__all__ = ["oversized_message_refusal"]
