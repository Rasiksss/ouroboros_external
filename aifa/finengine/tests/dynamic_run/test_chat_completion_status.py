from __future__ import annotations

import json
from pathlib import Path

import pytest

from finengine.contracts.transport import CompletedEvent
from finengine.skills import run_orchestrator


FIXTURES = Path(__file__).parent / "fixtures" / "sse"


def _terminal_events(name: str) -> list[dict]:
    events = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return [event for event in events if event["type"] in {"typed_result", "typed_error", "text_delta", "done"}]


class EventStream:
    def __init__(self, events: list[dict]) -> None:
        self.events = events

    def __call__(self, *args, **kwargs):  # noqa: ARG002
        return self._iterate()

    async def _iterate(self):
        for event in self.events:
            yield event


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["gave-up-as-answer.json", "stopped-with-partials.json"])
async def test_non_error_dynamic_outcome_does_not_finish_as_failed(monkeypatch, fixture: str) -> None:
    monkeypatch.setattr(run_orchestrator, "subscribe", EventStream(_terminal_events(fixture)))

    events = [event async for event in run_orchestrator.subscribe_versioned("run-1", kind="chat")]

    completed = next(event for event in events if isinstance(event, CompletedEvent))
    # frontend (assistant-answer-layout.test.ts) проверяет обработчик `done` именно с этим статусом
    assert completed.status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["dynamic_run_failed", "evidence_gate_failed"])
async def test_real_failures_still_finish_as_failed(monkeypatch, code: str) -> None:
    source = [
        {
            "type": "typed_error",
            "error": {"code": code, "message": "сбой", "retryable": False, "node_key": None, "details": {}},
            "partial_results": [],
        },
        {"type": "done"},
    ]
    monkeypatch.setattr(run_orchestrator, "subscribe", EventStream(source))

    events = [event async for event in run_orchestrator.subscribe_versioned("run-1", kind="chat")]

    completed = next(event for event in events if isinstance(event, CompletedEvent))
    assert completed.status == "failed"
