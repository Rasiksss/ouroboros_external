"""Минимальный клиент хода по WS для контрактных тестов (CONTRACTS.md §2.1–§2.3).

Временный: настоящий `ouro_client` службы появится в шаге 08 и заменит его в тестах. Здесь только
то, что нужно, чтобы пройти границу служба ↔ ouroboros: рукопожатие с заголовками владельца,
кадр `chat`, чтение кадров до финального ответа корня.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from websockets.sync.client import connect

# Заголовки рукопожатия — CONTRACTS.md §2.1. Ставит их в продукте только ouro_client (F12);
# этот клиент — тестовый двойник, в продукт не входит.
OWNER_HEADER = "X-Ouroboros-Owner-Id"
CHAT_HEADER = "X-Ouroboros-Chat-Id"
TRACE_HEADER = "X-Trace-Id"

Frame = dict[str, Any]


@dataclass(frozen=True)
class Turn:
    """Кадры одного хода в порядке прихода."""

    run_id: str
    frames: tuple[Frame, ...]

    @property
    def echo(self) -> Frame | None:
        """Эхо реплики пользователя: `role: user`, `client_message_id == run_id` (§2.3)."""
        return next(
            (
                f
                for f in self.frames
                if f.get("type") == "chat"
                and f.get("role") == "user"
                and f.get("client_message_id") == self.run_id
            ),
            None,
        )

    @property
    def final(self) -> Frame | None:
        """Финальный ответ: непрогрессный `assistant` с `transport.source_message_id == run_id`."""
        return next((f for f in self.frames if is_final(f, self.run_id)), None)

    @property
    def progress(self) -> tuple[Frame, ...]:
        return tuple(f for f in self.frames if is_progress(f))


def is_progress(frame: Frame) -> bool:
    return (
        frame.get("type") == "chat"
        and frame.get("role") == "assistant"
        and bool(frame.get("is_progress"))
    )


def is_final(frame: Frame, run_id: str) -> bool:
    transport = frame.get("transport") or {}
    return (
        frame.get("type") == "chat"
        and frame.get("role") == "assistant"
        and not frame.get("is_progress")
        and transport.get("source_message_id") == run_id
    )


class TurnTimeoutError(TimeoutError):
    """Финала нет в срок; в тексте — какие кадры пришли, чтобы не гадать."""


def run_turn(
    url: str,
    *,
    owner_id: str,
    chat_id: str,
    text: str,
    run_id: str,
    timeout_s: float = 90.0,
) -> Turn:
    """Один ход: рукопожатие, кадр `chat`, чтение до финала корня или срока."""
    headers = {OWNER_HEADER: owner_id, CHAT_HEADER: chat_id, TRACE_HEADER: f"contract-{run_id}"}
    frames: list[Frame] = []
    deadline = time.monotonic() + timeout_s
    with connect(url, additional_headers=headers, open_timeout=15) as ws:
        ws.send(
            json.dumps(
                {
                    "type": "chat",
                    "content": text,
                    "client_message_id": run_id,
                    "sender_session_id": "aifa",
                }
            )
        )
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                seen = [f"{f.get('type')}/{f.get('role')}/{f.get('is_progress')}" for f in frames]
                raise TurnTimeoutError(f"финала хода {run_id} нет за {timeout_s} с; кадры: {seen}")
            try:
                frame = json.loads(ws.recv(timeout=left))
            except TimeoutError:
                continue
            frames.append(frame)
            if is_final(frame, run_id):
                return Turn(run_id, tuple(frames))
