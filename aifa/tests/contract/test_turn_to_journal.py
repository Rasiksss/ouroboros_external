"""Сквозной тест шага 08: `turn_gateway` → ouroboros (`contract`) → журнал хода `run_orchestrator`.

Три настоящих объекта (PRINCIPLES.md §Б.3): события читаются реплеем журнала, как их читает ручка
`/chat/runs/{id}/events`. Подставная — только модель.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from finengine.assistant.events import SERVICE_EVENT
from finengine.config import settings
from finengine.contracts.transport import CompletedEvent, LegacyCompatibilityEvent, RunEvent
from finengine.skills import run_orchestrator
from finengine.store import bus, cache

from stand import SCENARIOS, ContractStand
from stub_model.client import StubModelAdmin
from turn_support import gateway_of, journal_of, start_turn

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def isolated_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Журнал и шина прогонов — во временном каталоге и в памяти, как в тестах службы."""
    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(settings, "redis_url", "")
    bus.reset_bus()
    cache._cached = None
    yield
    bus.reset_bus()
    cache._cached = None
    run_orchestrator._runs.clear()


def _shape(events: list[RunEvent]) -> list[str]:
    """Тип события журнала: у старых — `type` в конверте, у новых — `event_type`."""
    return [
        e.legacy_event["type"] if isinstance(e, LegacyCompatibilityEvent) else e.event_type
        for e in events
    ]


async def test_a_turn_without_tools_reaches_the_journal_as_the_screen_expects(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """Ответ без инструментов: «доставлено», `text_delta`, `text_segment_end`, `completed`."""
    stub.use_scenario(SCENARIOS / "hello.json")

    run_id = start_turn(gateway_of(stand), "привет")
    events = await journal_of(run_id)

    assert _shape(events) == [SERVICE_EVENT, "text_delta", "text_segment_end", "completed"]
    assert [e.sequence for e in events] == [1, 2, 3, 4]
    assert isinstance(events[1], LegacyCompatibilityEvent)
    assert events[1].legacy_event["text"] == "Здравствуйте! Это подставная модель."
    assert isinstance(events[-1], CompletedEvent) and events[-1].status == "succeeded"


async def test_the_stop_button_interrupts_the_turn_and_ouroboros_stops_asking_the_model(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """«Стоп»: `/stop`, `interrupted`, отмена в журнале; модель больше не спрашивают."""
    stub.use_scenario(SCENARIOS / "endless.json")
    run_id = start_turn(gateway_of(stand), "ход бесконечный")
    await asyncio.sleep(2.0)
    assert len(stub.journal()) > 3, "до «Стопа» ход идёт"

    assert run_orchestrator.cancel_run(run_id) is True
    events = await journal_of(run_id)

    shape = _shape(events)
    assert shape[-3:] == ["interrupted", "error", "completed"], shape
    assert isinstance(events[-1], CompletedEvent) and events[-1].status == "cancelled"
    halted = len(stub.journal())
    await asyncio.sleep(2.0)
    assert len(stub.journal()) == halted
