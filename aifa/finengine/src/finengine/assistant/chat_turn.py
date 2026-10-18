"""Ход чата через ouroboros: раннер для `run_orchestrator.start_run` (CONTRACTS.md §1, §2, шаг 10).

Ручка `POST /chat/turns` тонкая: она создаёт прогон и отдаёт его этому раннеру. Ход ведёт `TurnGateway`;
ветки OpenClaw (`aifa_gateway`, `station_turn`, `agent.query_stream`) отсюда не вызываются.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from finengine.assistant.events import Event
from finengine.assistant.session_turns import RootRecorder
from finengine.assistant.turn_gateway import TurnGateway, TurnRequest

ARCH = "ouroboros"


def chat_of(project_id: str | None, session_id: str) -> str:
    """Чат ouroboros = проект (ADR-0011, ADR-0014); хода без проекта чатом становится его сессия, пустого чата нет."""
    return project_id or session_id


@dataclass(frozen=True)
class OuroTurn:
    """Ход одного сообщения. `run` — ячейка с `run_id`: идентификатор выдаёт `start_run`, то есть позже раннера."""

    gateway: TurnGateway
    run: dict[str, str]
    owner_id: str
    chat_id: str
    session_id: str
    trace_id: str
    message: str
    user_id: int
    username: str
    project_id: str
    doc_ids: list[str]

    def __call__(self) -> AsyncGenerator[Event, None]:
        return self._events()

    async def _events(self) -> AsyncGenerator[Event, None]:
        started_at = time.time()
        answer: list[str] = []
        failure = ""
        # Первое событие хода у экрана всегда было `session_id`: по нему он запоминает сессию чата.
        yield {"type": "session_id", "session_id": self.session_id}
        try:
            request = TurnRequest(self.run["run_id"], self.owner_id, self.chat_id, self.message, self.trace_id)
            on_root = RootRecorder(self.owner_id, self.session_id)
            async for event in self.gateway.events(request, on_root=on_root):
                if event.get("type") == "text_delta":
                    answer.append(str(event.get("text") or ""))
                elif event.get("type") == "error":
                    failure = str(event.get("message") or event.get("error") or "")
                yield event
        finally:
            self._audit("".join(answer), failure, started_at)

    def _audit(self, response: str, failure: str, started_at: float) -> None:
        from finengine.skills.run_logger import log_chat_turn  # noqa: PLC0415 — тяжёлый слой журналов

        log_chat_turn(
            session_id=self.session_id, user_id=self.user_id, username=self.username, doc_ids=self.doc_ids,
            message=self.message, response=response, started_at=started_at, arch=ARCH, error=failure or None,
            project_id=self.project_id,
        )
