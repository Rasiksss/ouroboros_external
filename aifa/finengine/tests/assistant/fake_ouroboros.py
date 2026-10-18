"""Подставной сервер ouroboros для модульных тестов кадров хода: WS `/ws` и `GET /api/chat/history`.

Говорит по сети настоящим протоколом `websockets`, поэтому `OuroClient` и `TurnGateway` проходят
весь путь рукопожатия, кадров и обрыва. Контрактные тесты (`aifa/tests/contract`) идут на настоящий
ouroboros и подтверждают, что формы кадров ниже — его формы.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from types import TracebackType
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.http11 import Request, Response

Handler = Callable[[ServerConnection, "FakeOuroboros"], Awaitable[None]]


def echo(run_id: str, text: str) -> dict[str, Any]:
    return {"type": "chat", "role": "user", "content": text, "client_message_id": run_id}


def assistant(
    run_id: str, text: str, *, task_id: str = "", progress: bool = False, source: str | None = None
) -> dict[str, Any]:
    return {
        "type": "chat",
        "role": "assistant",
        "content": text,
        "is_progress": progress,
        "task_id": task_id,
        "transport": {"source_message_id": run_id if source is None else source},
    }


def history_record(
    role: str,
    text: str,
    run_id: str,
    *,
    task_id: str = "",
    progress: bool = False,
    ts: str = "2026-10-18T10:00:00+00:00",
) -> dict[str, Any]:
    return {
        "role": role,
        "text": text,
        "is_progress": progress,
        "client_message_id": run_id,
        "task_id": task_id,
        "ts": ts,
    }


class FakeOuroboros:
    """Сценарий соединения задаёт `handler(connection, server)`; номер соединения — `server.accepted`.

    `history` — ответы `GET /api/chat/history` по порядку вызовов, последний повторяется; число —
    код ошибки HTTP. `refuse_handshake` — рукопожатие закрывается до приёма (HTTP 403).
    """

    def __init__(
        self,
        handler: Handler,
        *,
        history: list[dict[str, Any] | int] | None = None,
        refuse_handshake: bool = False,
    ) -> None:
        self._handler = handler
        self._history = history or [{"messages": []}]
        self.refuse_handshake = refuse_handshake
        self.accepted = 0
        self.handshakes: list[dict[str, str]] = []
        self.history_calls: list[tuple[dict[str, str], dict[str, str]]] = []
        self.received: list[dict[str, Any]] = []
        self.accepting = True
        self._server: Server | None = None

    async def __aenter__(self) -> FakeOuroboros:
        self._server = await serve(
            self._serve, "127.0.0.1", 0, process_request=self._process_request, ping_interval=None
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        assert self._server is not None
        port = next(iter(self._server.sockets)).getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop_accepting(self) -> None:
        """Новые рукопожатия получают 503 — ouroboros лежит, а живые соединения не тронуты."""
        self.accepting = False

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        headers = {name.lower(): value for name, value in request.headers.raw_items()}
        if request.path.startswith("/api/chat/history"):
            return self._history_response(connection, request, headers)
        if not self.accepting:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "down")
        if self.refuse_handshake:
            return connection.respond(HTTPStatus.FORBIDDEN, "refused")
        self.handshakes.append(headers)
        return None

    def _history_response(
        self, connection: ServerConnection, request: Request, headers: dict[str, str]
    ) -> Response:
        query = request.path.partition("?")[2]
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        self.history_calls.append((headers, params))
        answer = self._history[min(len(self.history_calls), len(self._history)) - 1]
        if isinstance(answer, int):
            return connection.respond(HTTPStatus(answer), "history failed")
        response = connection.respond(HTTPStatus.OK, json.dumps(answer))
        response.headers["Content-Type"] = "application/json"
        return response

    async def _serve(self, connection: ServerConnection) -> None:
        self.accepted += 1
        await self._handler(connection, self)

    async def wait_received(self, count: int, timeout: float = 3.0) -> list[dict[str, Any]]:
        async with asyncio.timeout(timeout):
            while len(self.received) < count:
                await asyncio.sleep(0.01)
        return self.received

    async def read_until_closed(self, connection: ServerConnection) -> None:
        async for raw in connection:
            self.received.append(json.loads(raw))

    async def read_frame(self, connection: ServerConnection) -> dict[str, Any]:
        frame: dict[str, Any] = json.loads(await connection.recv())
        self.received.append(frame)
        return frame

    @staticmethod
    async def send(connection: ServerConnection, *frames: dict[str, Any]) -> None:
        for frame in frames:
            await connection.send(json.dumps(frame, ensure_ascii=False))
