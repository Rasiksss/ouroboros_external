"""`turn_gateway` в журнале хода `run_orchestrator`: «Стоп», отказы, ответ (CONTRACTS.md §2.5, §2.7).

Модульные тесты на подставном ouroboros; настоящий ouroboros — `aifa/tests/contract/test_turn_to_journal.py`.
Журнал читается реплеем `subscribe_versioned`, как его читает ручка `/chat/runs/{id}/events`.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fake_ouroboros import FakeOuroboros, assistant, echo
from turn_harness import TurnRunner, gateway_of
from websockets.asyncio.server import ServerConnection

from finengine.assistant.turn_gateway import TurnGateway
from finengine.contracts.transport import (
    CompletedEvent,
    ErrorEvent,
    LegacyCompatibilityEvent,
    RunEvent,
)
from finengine.skills import run_orchestrator as ro
from finengine.store import bus as B
from finengine.store import cache as C

STOP = {"type": "command", "cmd": "/stop", "client_message_id": "run-1-stop"}


@pytest.fixture(autouse=True)
def isolated_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from finengine.config import settings

    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(settings, "redis_url", "")
    B.reset_bus()
    C._cached = None
    yield
    B.reset_bus()
    C._cached = None
    ro._runs.clear()


async def journal_of(run_id: str) -> list[RunEvent]:
    return [event async for event in ro.subscribe_versioned(run_id, kind="chat")]


def name_of(event: RunEvent) -> str:
    """Служебное событие — по виду, прежнее — по типу, новое — по `event_type`."""
    if not isinstance(event, LegacyCompatibilityEvent):
        return event.event_type
    legacy = event.legacy_event
    return str(legacy["kind"] if legacy["type"] == "ouro_service" else legacy["type"])


def shape(events: list[RunEvent]) -> list[str]:
    return [name_of(event) for event in events]


def start(gateway: TurnGateway) -> str:
    runner = TurnRunner(gateway)
    # `start_run` выдаёт свой идентификатор; ход обязан нести его же (ADR-0021 п. 2).
    runner.run_id = ro.start_run(runner, kind="chat", meta={})
    return runner.run_id


async def quiet_after_echo(connection: ServerConnection, server: FakeOuroboros) -> None:
    asked = await server.read_frame(connection)
    await server.send(connection, echo(asked["client_message_id"], asked["content"]))
    await server.read_until_closed(connection)


async def answers_the_stop_with_a_final(connection: ServerConnection, server: FakeOuroboros) -> None:
    asked = await server.read_frame(connection)
    run_id = asked["client_message_id"]
    await server.send(connection, echo(run_id, asked["content"]))
    await server.read_frame(connection)
    await server.send(connection, assistant(run_id, "Остановлено.", task_id="r00t"))
    await server.read_until_closed(connection)


async def answers(connection: ServerConnection, server: FakeOuroboros) -> None:
    asked = await server.read_frame(connection)
    run_id = asked["client_message_id"]
    await server.send(
        connection,
        echo(run_id, asked["content"]),
        assistant(run_id, "Ответ ассистента.", task_id="r00t"),
    )
    await server.read_until_closed(connection)


async def test_the_stop_button_sends_stop_waits_and_ends_the_journal_as_cancelled() -> None:
    """§2.7: «Стоп» → `/stop`, ожидание финала до `stop_wait_sec`, `interrupted`; итог журнала — `cancelled`."""
    async with FakeOuroboros(quiet_after_echo) as server:
        run_id = start(gateway_of(server, stop_wait_sec=0.4))
        await server.wait_received(1)
        await asyncio.sleep(0.1)
        cancelled_at = time.monotonic()

        assert ro.cancel_run(run_id) is True
        events = await journal_of(run_id)
        waited = time.monotonic() - cancelled_at
        await server.wait_received(2)

    assert shape(events) == ["delivered", "interrupted", "error", "completed"]
    assert isinstance(events[-2], ErrorEvent) and isinstance(events[-1], CompletedEvent)
    assert events[-1].status == "cancelled"
    assert server.received[1] == {**STOP, "client_message_id": f"{run_id}-stop"}
    assert waited >= 0.35, "финала нет — ждали весь срок"


async def test_a_final_after_the_stop_ends_the_wait_early_and_is_not_shown() -> None:
    """§2.7: финал пришёл раньше 30 с — ждать дальше незачем; текст «Стопа» на экран не идёт."""
    async with FakeOuroboros(answers_the_stop_with_a_final) as server:
        run_id = start(gateway_of(server, stop_wait_sec=3.0))
        await server.wait_received(1)
        await asyncio.sleep(0.1)
        cancelled_at = time.monotonic()

        ro.cancel_run(run_id)
        events = await journal_of(run_id)

    assert time.monotonic() - cancelled_at < 1.5
    assert shape(events) == ["delivered", "interrupted", "error", "completed"]
    assert not any(
        isinstance(e, LegacyCompatibilityEvent) and e.legacy_event["type"] == "text_delta"
        for e in events
    )


async def test_an_unreachable_ouroboros_ends_the_journal_with_the_error_and_a_failed_completion() -> None:
    """§2.1: ход закрывается `error` с понятным текстом; `completed(failed)` дописывает реплей."""
    async with FakeOuroboros(answers) as server:
        server.stop_accepting()
        run_id = start(gateway_of(server))

        events = await journal_of(run_id)

    assert shape(events) == ["error", "completed"]
    assert isinstance(events[0], ErrorEvent)
    assert events[0].error.message == (
        "Ассистент сейчас недоступен. Повторите вопрос через несколько минут."
    )
    assert isinstance(events[1], CompletedEvent) and events[1].status == "failed"


async def test_an_answer_lands_in_the_journal_in_the_order_the_screen_expects() -> None:
    """Ответ без инструментов: `text_delta`, `text_segment_end`, `completed(succeeded)`."""
    async with FakeOuroboros(answers) as server:
        run_id = start(gateway_of(server))

        events = await journal_of(run_id)

    assert shape(events) == ["delivered", "text_delta", "text_segment_end", "completed"]
    assert [e.sequence for e in events] == [1, 2, 3, 4]
    assert isinstance(events[-1], CompletedEvent) and events[-1].status == "succeeded"
