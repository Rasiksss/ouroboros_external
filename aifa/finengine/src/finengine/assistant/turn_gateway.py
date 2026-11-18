"""Один ход через ouroboros: кадры → события журнала хода (CONTRACTS.md §2.3–§2.7, ADR-0021).

`TurnGateway.events` — асинхронный генератор событий для `run_orchestrator.start_run`: ход живёт в
фоновом прогоне, «Стоп» приходит отменой его задачи.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from finengine.assistant import messages
from finengine.assistant.events import (
    Event,
    answer_events,
    error_event,
    interrupted_event,
    lost_events,
    service_event,
)
from finengine.assistant.frames import SUBTASK_FAILURE_PREFIX, Frame, FrameKind, classify
from finengine.assistant.history import HistoryPage, find_final
from finengine.assistant.plan_view import root_plan_event
from finengine.assistant.ouro_client import (
    ChatScope,
    HandshakeRefused,
    LinkLost,
    MessageTooLong,
    OuroClient,
    OuroError,
    OuroUnavailable,
    TurnSocket,
)
from finengine.config import Settings
from finengine.skills import run_orchestrator

log = logging.getLogger(__name__)

ROOT_COMMAND = "root"

# После крайнего срока ouroboros сам прерывает задачу (HARD_TIMEOUT) и отвечает; запас — на это ответ.
DEADLINE_GRACE_SEC = 60.0


@dataclass(frozen=True)
class TurnRequest:
    """Ход: `run_id` службы он же `client_message_id` (ADR-0021 п. 2)."""

    run_id: str
    owner_id: str
    chat_id: str
    text: str
    trace_id: str


@dataclass(frozen=True)
class TurnTimings:
    deadline_sec: float
    silence_sec: float
    stop_wait_sec: float
    retry_pause_sec: float
    reconnect_attempts: int

    @classmethod
    def from_settings(cls, settings: Settings) -> TurnTimings:
        return cls(
            deadline_sec=settings.ouroboros_hard_timeout_sec + DEADLINE_GRACE_SEC,
            silence_sec=settings.ouroboros_silence_sec,
            stop_wait_sec=settings.ouroboros_stop_wait_sec,
            retry_pause_sec=settings.ouroboros_retry_pause_sec,
            reconnect_attempts=settings.ouroboros_reconnect_attempts,
        )


@dataclass(frozen=True)
class SubtaskResult:
    """Подзадача хода завершилась: её итог пришёл кадром (ADR-0041 п. 3, ADR-0043 п. 3)."""

    run_id: str
    task_id: str
    text: str
    failed: bool


class SubtaskSink(Protocol):
    """Точка расширения: потребитель — `aifa_wait(task_id)` (шаг 16)."""

    def subtask_finished(self, result: SubtaskResult) -> None: ...


class RootSink(Protocol):
    """Кому сказать корень хода, когда ход закончен: история отбирает финал по нему (§2.6)."""

    def __call__(self, run_id: str, task_id: str) -> None: ...


class TurnGateway:
    def __init__(
        self, client: OuroClient, timings: TurnTimings, subtasks: SubtaskSink | None = None
    ) -> None:
        self._client = client
        self._timings = timings
        self._subtasks = subtasks
        self._live: dict[str, _Turn] = {}

    def root_known(self, run_id: str, task_id: str) -> bool:
        """Корень хода назван вызовом инструмента (`root_task_id` пуст → `task_id`, §2.4)."""
        turn = self._live.get(run_id)
        return turn is not None and turn.know_root(task_id)

    async def events(self, request: TurnRequest, *, on_root: RootSink | None = None) -> AsyncIterator[Event]:
        turn = _Turn(self._client, self._timings, self._subtasks, request)
        self._live[request.run_id] = turn
        run = run_orchestrator.get_run(request.run_id)
        if run is not None:
            # P52: вызов инструмента может прийти на другую реплику — она назовёт корень каналом управления.
            run.commands[ROOT_COMMAND] = NameRoot(turn)
        try:
            async for event in turn.events():
                yield event
        finally:
            del self._live[request.run_id]
            if on_root is not None and turn.root_task_id:
                on_root(request.run_id, turn.root_task_id)


@dataclass(frozen=True)
class NameRoot:
    """Команда `root` владельцу прогона: корень хода назван вызовом инструмента (CONTRACTS.md §3.0)."""

    turn: _Turn

    def __call__(self, message: dict[str, Any]) -> None:
        task_id = str(message.get("task_id") or "")
        if task_id:
            self.turn.know_root(task_id)


class _Turn:
    def __init__(
        self,
        client: OuroClient,
        timings: TurnTimings,
        subtasks: SubtaskSink | None,
        request: TurnRequest,
    ) -> None:
        self._client = client
        self._timings = timings
        self._subtasks = subtasks
        self._request = request
        self._scope = ChatScope(request.owner_id, request.chat_id)
        self._link: TurnSocket | None = None
        self._root_task_id = ""
        self._finished = False
        self._stop_needed = False
        self._deadline = asyncio.get_running_loop().time() + timings.deadline_sec

    @property
    def root_task_id(self) -> str:
        return self._root_task_id

    def know_root(self, task_id: str) -> bool:
        if self._root_task_id and self._root_task_id != task_id:
            log.warning("ход %s: корень назван дважды", self._request.run_id[:8])
            return False
        self._root_task_id = task_id
        return True

    async def events(self) -> AsyncIterator[Event]:
        try:
            async for event in self._conduct():
                yield event
        except asyncio.CancelledError:
            async for event in self._interrupt():
                yield event
            raise
        finally:
            await self._close()

    async def _conduct(self) -> AsyncIterator[Event]:
        failure = await self._open_and_send()
        if failure is not None:
            yield error_event(failure)
            return
        async for event in self._read():
            yield event
        if self._stop_needed:
            await self._stop_quietly()

    async def _open_and_send(self) -> str | None:
        """Текст отказа для пользователя или `None`: соединение открыто, вопрос отправлен."""
        request = self._request
        try:
            self._link = await self._client.open_turn(self._scope, request.trace_id)
            await self._link.send_chat(request.text, request.run_id)
        except HandshakeRefused:
            return messages.REFUSED
        except MessageTooLong as too_long:
            return messages.too_long(too_long.actual, too_long.limit)
        except (OuroUnavailable, LinkLost) as failure:
            log.warning("ход %s: ассистент недоступен: %s", request.run_id[:8], failure)
            return messages.UNAVAILABLE
        return None

    async def _read(self) -> AsyncIterator[Event]:
        loop = asyncio.get_running_loop()
        while not self._finished:
            left = self._deadline - loop.time()
            if left <= 0:
                async for event in self._at_deadline():
                    yield event
                return
            wait = min(self._timings.silence_sec, left)
            try:
                frame = await self._receive(wait)
            except TimeoutError:
                # Ждали крайнего срока, а не тишины. По `loop.time()` этого не понять: таймер asyncio
                # срабатывает раньше на разрешение часов (на Windows 15,6 мс), и срок «ещё не наступил».
                if wait < self._timings.silence_sec or loop.time() >= self._deadline:
                    async for event in self._at_deadline():
                        yield event
                    return
                async for event in self._recover():
                    yield event
                continue
            except LinkLost:
                if loop.time() < self._deadline:
                    async for event in self._recover():
                        yield event
                continue
            for event in self._take(frame):
                yield event

    async def _receive(self, timeout: float) -> Frame:
        assert self._link is not None
        return await self._link.recv(timeout)

    def _take(self, frame: Frame) -> list[Event]:
        """Кадр по таблице §2.3: события журнала; `self._finished` — ход закончен."""
        kind = classify(frame, run_id=self._request.run_id, root_task_id=self._root_task_id)
        files = _attachment_events(frame) if kind in (FrameKind.FINAL, FrameKind.NOTIFICATION) else []
        return files + self._dispatch(frame, kind)

    def _dispatch(self, frame: Frame, kind: FrameKind) -> list[Event]:
        text = str(frame.get("content") or "")
        task_id = str(frame.get("task_id") or "")
        if kind is FrameKind.FINAL:
            self._root_task_id = task_id
            return self._finish_with(text)
        if kind is FrameKind.SUBTASK_RESULT:
            return self._subtask_finished(task_id, text)
        if kind is FrameKind.ERROR:
            return self._transport_error(frame)
        if kind is FrameKind.CONFIRMATION:
            return self._confirmation_asked(frame)
        if kind is FrameKind.ECHO:
            return [service_event("delivered")]
        if kind is FrameKind.PROGRESS:
            return [service_event("progress", task_id=task_id, text=text)]
        if kind is FrameKind.NOTIFICATION:
            return [service_event("notification", text=text)]
        if kind is FrameKind.UNEXPECTED:
            return [service_event("unexpected", frame_type=str(frame.get("type") or ""))]
        return []

    def _finish_with(self, text: str) -> list[Event]:
        self._finished = True
        return answer_events(text)

    def _subtask_finished(self, task_id: str, text: str) -> list[Event]:
        failed = text.startswith(SUBTASK_FAILURE_PREFIX)
        if self._subtasks is not None:
            self._subtasks.subtask_finished(
                SubtaskResult(self._request.run_id, task_id, text, failed)
            )
        events = [service_event("subtask_result", task_id=task_id, failed=failed, text=text)]
        plan = None if failed else root_plan_event(task_id, text)
        return events if plan is None else [*events, plan]

    def _transport_error(self, frame: Frame) -> list[Event]:
        # Отказ ouroboros принять кадр: ответа на него не будет.
        self._finished = True
        code = str(frame.get("code") or "")
        shown = (
            messages.too_long(len(self._request.text.strip()), self._client.max_message_chars)
            if code == "message_too_long"
            else messages.REJECTED
        )
        return [service_event("transport_error", code=code), error_event(shown)]

    def _confirmation_asked(self, frame: Frame) -> list[Event]:
        # Подтверждение выключено настройками (ADR-0010); пришедший запрос — нарушение конфигурации.
        self._finished = True
        self._stop_needed = True
        tool = str(frame.get("tool") or "")
        log.error("ход %s: ouroboros запросил подтверждение (%s)", self._request.run_id[:8], tool)
        return [service_event("config_violation", tool=tool), error_event(messages.UNCONFIGURED)]

    async def _recover(self) -> AsyncIterator[Event]:
        """Обрыв (§2.7): переподключиться, поискать финал в истории, иначе `session_lost`."""
        await self._close()
        for _ in range(self._timings.reconnect_attempts):
            if not await self._pause():
                break
            if not await self._reconnect():
                continue
            # Пока соединения не было, кадры терялись для живой доставки; история их помнит.
            found = await self._final_in_history()
            if found is not None:
                for event in self._finish_with(found):
                    yield event
            return
        while await self._pause():
            found = await self._final_in_history()
            if found is not None:
                for event in self._finish_with(found):
                    yield event
                return
        self._finished = True
        for event in lost_events():
            yield event

    async def _reconnect(self) -> bool:
        try:
            self._link = await self._client.open_turn(self._scope, self._request.trace_id)
        except OuroError:
            return False
        return True

    async def _pause(self) -> bool:
        """Подождать между попытками; `False` — крайний срок хода наступил."""
        loop = asyncio.get_running_loop()
        left = self._deadline - loop.time()
        if left <= 0:
            return False
        await asyncio.sleep(min(self._timings.retry_pause_sec, left))
        return loop.time() < self._deadline

    async def _final_in_history(self) -> str | None:
        try:
            record = await find_final(
                self._history_page, run_id=self._request.run_id, root_task_id=self._root_task_id
            )
        except OuroUnavailable as failure:
            log.warning("ход %s: история недоступна: %s", self._request.run_id[:8], failure)
            return None
        if record is None:
            return None
        self._root_task_id = record.task_id
        return record.text

    async def _history_page(self, before_ts: str) -> HistoryPage:
        return await self._client.history(
            self._scope, before_ts=before_ts, trace_id=self._request.trace_id
        )

    async def _at_deadline(self) -> AsyncIterator[Event]:
        found = await self._final_in_history()
        if found is not None:
            for event in self._finish_with(found):
                yield event
            return
        self._finished = True
        self._stop_needed = True
        yield error_event(messages.TIMED_OUT)

    async def _interrupt(self) -> AsyncIterator[Event]:
        """«Стоп» (§2.7): `/stop`, ждём финала до `stop_wait_sec`, затем `interrupted`."""
        if self._link is not None:
            try:
                await self._link.send_stop(self._request.run_id)
            except OuroError:
                pass
            else:
                await self._await_final()
        yield interrupted_event()

    async def _await_final(self) -> None:
        # Финал после «Стопа» экрану не нужен: ждём только, чтобы корень успел остановиться.
        try:
            async with asyncio.timeout(self._timings.stop_wait_sec):
                while True:
                    frame = await self._receive(self._timings.stop_wait_sec)
                    kind = classify(
                        frame, run_id=self._request.run_id, root_task_id=self._root_task_id
                    )
                    if kind is FrameKind.FINAL:
                        return
        except (TimeoutError, LinkLost):
            return

    async def _stop_quietly(self) -> None:
        if self._link is None:
            return
        try:
            await self._link.send_stop(self._request.run_id)
        except OuroError:
            log.warning("ход %s: /stop не доставлен", self._request.run_id[:8])

    async def _close(self) -> None:
        link, self._link = self._link, None
        if link is not None:
            await link.close()


def _attachment_events(frame: Frame) -> list[Event]:
    """Вложения кадра ассистента — файлы каталога ouroboros, которые агент отправил сам (ADR-0059)."""
    attachments = frame.get("attachments")
    if not isinstance(attachments, list):
        return []
    return [
        service_event(
            "attachment", catalog_file_id=str(item["id"]), filename=str(item.get("filename") or ""),
            mime=str(item.get("mime") or ""), size=int(item.get("size") or 0),
        )
        for item in attachments
        if isinstance(item, dict) and item.get("id") and item.get("status") == "ready"
    ]
