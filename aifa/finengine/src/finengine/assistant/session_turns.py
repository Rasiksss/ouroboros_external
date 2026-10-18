"""Ходы сессии экрана: в какой чат ouroboros ушёл каждый ход и кто его корень (CONTRACTS.md §2.6).

История сессии — сообщения её ходов, а не всего чата: чат папки делят все её сессии (`chat_of` получает
проект папки), а сессию можно перенести в папку, и её ходы окажутся в двух чатах. Поэтому служба помнит
`run_id` своих ходов вместе с чатом и корнем (§2.6). Запись живёт в общем состоянии службы (`store/state`,
база): дольше кэша хода (`TurnRecord`, 6 часов) — столько же, сколько история.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from finengine.store import state

log = logging.getLogger(__name__)

TURN_LIMIT = 500  # последних ходов сессии: история отдаёт 200 сообщений, то есть не больше 100 ходов


@dataclass(frozen=True)
class SessionTurn:
    run_id: str
    chat_id: str
    root_task_id: str  # "" — корень ещё не назван (ход идёт или оборвался)


def load(owner_id: str, session_id: str) -> tuple[SessionTurn, ...]:
    """Ходы сессии у этого владельца по порядку; пусто — у владельца такой сессии нет."""
    doc = state.get(state.CHAT_SESSION, owner=owner_id, key=session_id) or {}
    return tuple(
        SessionTurn(str(turn.get("run_id") or ""), str(turn.get("chat_id") or ""), str(turn.get("root_task_id") or ""))
        for turn in doc.get("turns") or []
        if isinstance(turn, dict) and turn.get("run_id") and turn.get("chat_id")
    )


def by_chat(turns: tuple[SessionTurn, ...]) -> dict[str, dict[str, str]]:
    """Ходы, сгруппированные по чату ouroboros: чат → `run_id` → корень (вход `history.session_messages`)."""
    chats: dict[str, dict[str, str]] = {}
    for turn in turns:
        chats.setdefault(turn.chat_id, {})[turn.run_id] = turn.root_task_id
    return chats


def record_turn(owner_id: str, session_id: str, *, run_id: str, chat_id: str) -> None:
    """Ход начат: запомнить его чат до первого кадра, иначе история не узнает реплику этого хода."""
    state.mutate(
        state.CHAT_SESSION, owner=owner_id, key=session_id, изменить=_TurnAdded(run_id, chat_id),
    )


@dataclass(frozen=True)
class _TurnAdded:
    run_id: str
    chat_id: str

    def __call__(self, doc: dict[str, Any] | None) -> dict[str, Any]:
        turns = [turn for turn in (doc or {}).get("turns") or [] if turn.get("run_id") != self.run_id]
        turns.append({"run_id": self.run_id, "chat_id": self.chat_id, "root_task_id": ""})
        return {"turns": turns[-TURN_LIMIT:]}


@dataclass(frozen=True)
class _RootNamed:
    run_id: str
    task_id: str

    def __call__(self, doc: dict[str, Any] | None) -> dict[str, Any]:
        turns = [
            {**turn, "root_task_id": self.task_id} if turn.get("run_id") == self.run_id else turn
            for turn in (doc or {}).get("turns") or []
        ]
        return {"turns": turns}


@dataclass(frozen=True)
class RootRecorder:
    """Корень хода, которым шлюз закончил ход (`TurnGateway.events(on_root=…)`), — в запись сессии."""

    owner_id: str
    session_id: str

    def __call__(self, run_id: str, task_id: str) -> None:
        try:
            state.mutate(
                state.CHAT_SESSION, owner=self.owner_id, key=self.session_id,
                изменить=_RootNamed(run_id, task_id),
            )
        except (OSError, SQLAlchemyError):
            # Ответ хода уже выдан: без корня история отберёт финал по порядку (`history.turn_messages`).
            log.warning("ход %s: корень не записан в сессию", run_id[:8], exc_info=True)
