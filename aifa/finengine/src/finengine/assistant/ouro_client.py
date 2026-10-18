"""Единственное место, где служба говорит с ouroboros по сети (CONTRACTS.md §2, F12).

Заголовки владельца ставит только этот модуль (ADR-0014); `X-Ouroboros-Agent-Id` не передаётся —
агент по умолчанию `main` (ADR-0002). Кадр `chat` уходит строго по §2.2: поля вне контракта —
исключение до отправки (F6, ADR-0040).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from finengine.assistant.history import HistoryPage, parse_page
from finengine.config import Settings

OWNER_HEADER = "X-Ouroboros-Owner-Id"
CHAT_HEADER = "X-Ouroboros-Chat-Id"
TRACE_HEADER = "X-Trace-Id"

# §2.2: у кадра хода нет других полей. Список разрешённых, а не запрещённых: вложения и добавки к
# тексту нельзя назвать заранее, а ouroboros принимает любой лишний ключ молча.
CHAT_FRAME_KEYS = frozenset({"type", "content", "client_message_id", "sender_session_id"})
STOP_FRAME_KEYS = frozenset({"type", "cmd", "client_message_id"})
SENDER = "aifa"

# Ответ ouroboros закрывает неприсланную личность до приёма соединения — это HTTP 403.
_REFUSED_STATUSES = frozenset({401, 403})
_FRAME_LIMIT_BYTES = 16 * 1024 * 1024
HISTORY_PATH = "/api/chat/history"
HISTORY_PAGE_SIZE = 50


class OuroError(Exception):
    """Отказ границы со стороны ouroboros; для пользователя — один из текстов `messages`."""


class OuroUnavailable(OuroError):
    """Не дозвонились, оборвали или ответили не так, как положено."""


class HandshakeRefused(OuroError):
    """Рукопожатие закрыто: личность не принята (§2.1)."""


class LinkLost(OuroError):
    """Соединение хода закрыто."""


class MessageTooLong(OuroError):
    def __init__(self, actual: int, limit: int) -> None:
        super().__init__(f"{actual} знаков при допустимых {limit}")
        self.actual = actual
        self.limit = limit


class ForbiddenFrame(OuroError):
    """В кадре поля вне контракта §2.2: служба такой кадр не отправляет."""


@dataclass(frozen=True)
class ChatScope:
    """Чья беседа: `str(user.id)` и идентификатор проекта (ADR-0014)."""

    owner_id: str
    chat_id: str


def _headers(scope: ChatScope, trace_id: str) -> dict[str, str]:
    return {OWNER_HEADER: scope.owner_id, CHAT_HEADER: scope.chat_id, TRACE_HEADER: trace_id}


def _check_frame(frame: Mapping[str, Any], allowed: frozenset[str]) -> None:
    extra = sorted(set(frame) - allowed)
    if extra:
        raise ForbiddenFrame(f"в кадре хода поля вне контракта §2.2: {extra}")


class TurnSocket:
    """Соединение одного хода (ADR-0021 п. 1)."""

    def __init__(self, connection: ClientConnection, max_chars: int) -> None:
        self._connection = connection
        self._max_chars = max_chars

    async def send_frame(self, frame: Mapping[str, Any]) -> None:
        _check_frame(frame, CHAT_FRAME_KEYS if frame.get("type") == "chat" else STOP_FRAME_KEYS)
        try:
            await self._connection.send(json.dumps(frame, ensure_ascii=False))
        except ConnectionClosed as closed:
            raise LinkLost("соединение хода закрыто") from closed

    async def send_chat(self, text: str, run_id: str) -> None:
        # Длину ouroboros считает по тексту без крайних пробелов.
        length = len(text.strip())
        if length > self._max_chars:
            raise MessageTooLong(length, self._max_chars)
        await self.send_frame(
            {"type": "chat", "content": text, "client_message_id": run_id, "sender_session_id": SENDER}
        )

    async def send_stop(self, run_id: str) -> None:
        # Свой `client_message_id`: ответ на команду несёт его и не принимается за кадр хода.
        await self.send_frame({"type": "command", "cmd": "/stop", "client_message_id": f"{run_id}-stop"})

    async def recv(self, timeout: float) -> dict[str, Any]:
        """Следующий кадр; `TimeoutError` — тишина дольше `timeout`, `LinkLost` — соединение закрыто."""
        try:
            raw = await asyncio.wait_for(self._connection.recv(), timeout)
        except ConnectionClosed as closed:
            raise LinkLost("соединение хода закрыто") from closed
        frame = _decode(raw)
        return frame if frame is not None else {"type": "unreadable"}

    async def close(self) -> None:
        await self._connection.close()


def _decode(raw: str | bytes) -> dict[str, Any] | None:
    try:
        frame = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return frame if isinstance(frame, dict) else None


class OuroClient:
    def __init__(self, base_url: str, *, connect_timeout_sec: float, max_message_chars: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._connect_timeout = connect_timeout_sec
        self._max_chars = max_message_chars

    @classmethod
    def from_settings(cls, settings: Settings) -> OuroClient:
        return cls(
            settings.ouroboros_url,
            connect_timeout_sec=settings.ouroboros_connect_timeout_sec,
            max_message_chars=settings.ouroboros_max_inbound_chars,
        )

    @property
    def max_message_chars(self) -> int:
        return self._max_chars

    @property
    def _ws_url(self) -> str:
        return self._base_url.replace("http", "ws", 1) + "/ws"

    async def open_turn(self, scope: ChatScope, trace_id: str) -> TurnSocket:
        try:
            connection = await connect(
                self._ws_url,
                additional_headers=_headers(scope, trace_id),
                open_timeout=self._connect_timeout,
                max_size=_FRAME_LIMIT_BYTES,
                # Живость держат биения ouroboros и тишина, которую считает ход, а не пинги клиента.
                ping_interval=None,
            )
        except InvalidStatus as refused:
            if refused.response.status_code in _REFUSED_STATUSES:
                raise HandshakeRefused("рукопожатие закрыто") from refused
            raise OuroUnavailable(f"рукопожатие: {refused.response.status_code}") from refused
        except (OSError, TimeoutError, WebSocketException) as failure:
            raise OuroUnavailable(type(failure).__name__) from failure
        return TurnSocket(connection, self._max_chars)

    async def history(
        self, scope: ChatScope, *, before_ts: str = "", trace_id: str = ""
    ) -> HistoryPage:
        """Страница истории чата (§2.6), новые записи первыми страницами; дальше — `before_ts`."""
        params = {"limit": str(HISTORY_PAGE_SIZE), **({"before_ts": before_ts} if before_ts else {})}
        headers = {name: value for name, value in _headers(scope, trace_id).items() if value}
        try:
            async with httpx.AsyncClient(timeout=self._connect_timeout) as http:
                response = await http.get(self._base_url + HISTORY_PATH, params=params, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as failure:
            raise OuroUnavailable(type(failure).__name__) from failure
        return parse_page(payload)
