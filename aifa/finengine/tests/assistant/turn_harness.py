"""Общее для модульных тестов хода: сроки, потребитель подзадач, запуск хода до конца."""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace

from fake_ouroboros import FakeOuroboros

from finengine.assistant.events import Event
from finengine.assistant.ouro_client import OuroClient
from finengine.assistant.turn_gateway import (
    SubtaskResult,
    TurnGateway,
    TurnRequest,
    TurnTimings,
)

TIMINGS = TurnTimings(
    deadline_sec=5.0, silence_sec=0.5, stop_wait_sec=0.3, retry_pause_sec=0.05, reconnect_attempts=2
)


@dataclass
class Subtasks:
    results: list[SubtaskResult] = field(default_factory=list)

    def subtask_finished(self, result: SubtaskResult) -> None:
        self.results.append(result)


@dataclass
class NameRootOnDelivery:
    """Роль `tool_api` (шаг 09): корень называют, как только ход начался."""

    gateway: TurnGateway
    run_id: str
    task_id: str

    def __call__(self, event: Event) -> None:
        if event.get("kind") == "delivered":
            self.gateway.root_known(self.run_id, self.task_id)


def request_of(text: str = "вопрос", run_id: str = "run-1") -> TurnRequest:
    return TurnRequest(run_id, "7", "project-1", text, "trace-1")


def gateway_of(server: FakeOuroboros, subtasks: Subtasks | None = None, **limits: float) -> TurnGateway:
    client = OuroClient(server.url, connect_timeout_sec=1.0, max_message_chars=100)
    return TurnGateway(client, replace(TIMINGS, **limits), subtasks)


async def drain(
    gateway: TurnGateway, request: TurnRequest, watch: NameRootOnDelivery | None = None
) -> list[Event]:
    events: list[Event] = []
    async for event in gateway.events(request):
        events.append(event)
        if watch is not None:
            watch(event)
    return events


def shape(events: list[Event]) -> list[str]:
    return [str(e.get("kind") or e["type"]) for e in events]


@dataclass
class TurnRunner:
    """Раннер `run_orchestrator.start_run`: `run_id` ему сообщают после старта, как `_run_cell` в api/main.py."""

    gateway: TurnGateway
    text: str = "вопрос"
    run_id: str = ""

    def __call__(self) -> AsyncIterator[Event]:
        return self.gateway.events(request_of(self.text, self.run_id))
