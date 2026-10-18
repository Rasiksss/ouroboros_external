"""Общее для контрактных тестов хода (CONTRACTS.md §2): настоящие `OuroClient` и `TurnGateway`."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from finengine.assistant.events import SERVICE_EVENT, Event
from finengine.assistant.frames import Frame, FrameKind, classify
from finengine.assistant.ouro_client import ChatScope, OuroClient, TurnSocket
from finengine.assistant.turn_gateway import (
    SubtaskSink,
    TurnGateway,
    TurnRequest,
    TurnTimings,
)
from finengine.contracts.transport import RunEvent
from finengine.skills import run_orchestrator

from stand import ContractStand

OWNER = "contract-owner"
MAX_CHARS = 50_000
# Срок хода и ожидание «Стопа» короче продовых: подставная модель отвечает за секунды, а после
# `/stop` ouroboros финала не присылает (CONTRACTS.md §2.7) — ждать приходится весь срок.
TIMINGS = TurnTimings(
    deadline_sec=90.0,
    silence_sec=60.0,
    stop_wait_sec=3.0,
    retry_pause_sec=0.2,
    reconnect_attempts=2,
)


def client_of(stand: ContractStand) -> OuroClient:
    return OuroClient(stand.ouro_http_url, connect_timeout_sec=10.0, max_message_chars=MAX_CHARS)


def gateway_of(stand: ContractStand, subtasks: SubtaskSink | None = None) -> TurnGateway:
    return TurnGateway(client_of(stand), TIMINGS, subtasks)


def new_chat() -> str:
    return f"turn-{uuid.uuid4().hex[:8]}"


def new_request(text: str, *, chat_id: str | None = None, owner_id: str = OWNER) -> TurnRequest:
    return TurnRequest(
        run_id=uuid.uuid4().hex,
        owner_id=owner_id,
        chat_id=chat_id or new_chat(),
        text=text,
        trace_id=str(uuid.uuid4()),
    )


def scope_of(request: TurnRequest) -> ChatScope:
    return ChatScope(request.owner_id, request.chat_id)


async def collect(
    gateway: TurnGateway,
    request: TurnRequest,
    on_event: Callable[[Event], None] | None = None,
) -> list[Event]:
    """Все события хода по порядку; `on_event` видит каждое, пока генератор ждёт следующего."""
    events: list[Event] = []
    async for event in gateway.events(request):
        events.append(event)
        if on_event is not None:
            on_event(event)
    return events


def service(events: list[Event], kind: str) -> list[Event]:
    return [e for e in events if e["type"] == SERVICE_EVENT and e["kind"] == kind]


def screen(events: list[Event]) -> list[Event]:
    """События, которые видит экран: без служебных."""
    return [e for e in events if e["type"] != SERVICE_EVENT]


async def frames_until(
    link: TurnSocket,
    request: TurnRequest,
    stop: Callable[[FrameKind, Frame], bool],
    *,
    root_task_id: str = "",
    timeout_sec: float = 30.0,
) -> list[Frame]:
    """Кадры хода, пока `stop(вид, кадр)` не вернёт истину; не дождались в срок — ошибка."""
    frames: list[Frame] = []
    deadline = time.monotonic() + timeout_sec
    while (left := deadline - time.monotonic()) > 0:
        frame = await link.recv(left)
        frames.append(frame)
        kind = classify(frame, run_id=request.run_id, root_task_id=root_task_id)
        if stop(kind, frame):
            return frames
    shape = [f"{f.get('type')}/{f.get('role')}/{f.get('is_progress')}" for f in frames]
    raise TimeoutError(f"условие не наступило за {timeout_sec} с; кадры: {shape}")


@dataclass
class TurnRunner:
    """Раннер для `start_run`: `run_id` ему сообщают после старта, как `_run_cell` в api/main.py."""

    gateway: TurnGateway
    text: str
    chat_id: str
    owner_id: str = OWNER
    run_id: str = ""

    def __call__(self) -> AsyncIterator[Event]:
        request = TurnRequest(
            self.run_id, self.owner_id, self.chat_id, self.text, str(uuid.uuid4())
        )
        events: AsyncIterator[Event] = self.gateway.events(request)
        return events


def start_turn(gateway: TurnGateway, text: str, *, chat_id: str | None = None) -> str:
    """Ход как фоновый прогон журнала `chat`; возвращает `run_id`. Вызывать из работающего цикла."""
    runner = TurnRunner(gateway, text, chat_id or new_chat())
    runner.run_id = run_orchestrator.start_run(runner, kind="chat", meta={})
    return runner.run_id


async def journal_of(run_id: str) -> list[RunEvent]:
    """Журнал хода так, как его реплеит `/chat/runs/{id}/events` (A11); ждёт закрытия прогона."""
    return [event async for event in run_orchestrator.subscribe_versioned(run_id, kind="chat")]


@dataclass(frozen=True)
class IsKind:
    """Условие остановки `frames_until`: пришёл кадр этого вида."""

    wanted: FrameKind

    def __call__(self, kind: FrameKind, frame: Frame) -> bool:
        return kind is self.wanted
