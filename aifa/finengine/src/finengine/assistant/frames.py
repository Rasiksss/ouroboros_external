"""Что значит кадр ouroboros для хода (CONTRACTS.md §2.3, §2.4)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

Frame = dict[str, Any]

# «Подзадача упала» ouroboros сообщает обычным итогом с этим началом текста (ADR-0043 п. 3).
SUBTASK_FAILURE_PREFIX = "⚠️ Error during processing"


class FrameKind(StrEnum):
    ECHO = "echo"
    PROGRESS = "progress"
    FINAL = "final"
    SUBTASK_RESULT = "subtask_result"
    NOTIFICATION = "notification"
    TYPING = "typing"
    HEARTBEAT = "heartbeat"
    ERROR = "error"
    CONFIRMATION = "confirmation_request"
    FOREIGN = "foreign"
    UNEXPECTED = "unexpected"


_PLAIN = {
    "typing": FrameKind.TYPING,
    "heartbeat": FrameKind.HEARTBEAT,
    "error": FrameKind.ERROR,
    "confirmation_request": FrameKind.CONFIRMATION,
}


def source_message_id(frame: Frame) -> str:
    transport = frame.get("transport")
    return str(transport.get("source_message_id") or "") if isinstance(transport, dict) else ""


def classify(frame: Frame, *, run_id: str, root_task_id: str) -> FrameKind:
    """Роль кадра в ходе `run_id`. Пустой `root_task_id` — корень ещё не опознан (§2.4)."""
    kind = _PLAIN.get(str(frame.get("type") or ""))
    if kind is not None:
        return kind
    if frame.get("type") != "chat":
        return FrameKind.UNEXPECTED
    if frame.get("role") == "user":
        return FrameKind.ECHO if frame.get("client_message_id") == run_id else FrameKind.FOREIGN
    if frame.get("role") != "assistant":
        return FrameKind.UNEXPECTED
    source = source_message_id(frame)
    # Кадры другого хода того же чата (и ответ на `/stop`) приходят в то же соединение.
    if source and source != run_id:
        return FrameKind.FOREIGN
    if frame.get("is_progress"):
        return FrameKind.PROGRESS
    task_id = str(frame.get("task_id") or "")
    if not task_id:
        return FrameKind.NOTIFICATION
    if source != run_id:
        return FrameKind.UNEXPECTED
    if not root_task_id or task_id == root_task_id:
        return FrameKind.FINAL
    return FrameKind.SUBTASK_RESULT
