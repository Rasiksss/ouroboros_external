"""Итог хода после отказа конвейера: ответ ouroboros закрывает ход «успешно» (шаг 10, P62).

Отказ стандартного пути (`typed_error`) остаётся в журнале — карточка с частичными результатами
нужна экрану, — но статус хода решает то, что пользователь получил ответ. Прежние ходы без ответа
по-прежнему кончаются `failed`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from finengine.assistant.events import answer_events
from finengine.contracts.transport import CompletedEvent, ErrorEvent, LegacyCompatibilityEvent
from finengine.skills import run_orchestrator as ro
from finengine.skills.result_events import typed_error_event
from finengine.store import bus as B
from finengine.store import cache as C

REFUSAL = typed_error_event(code="metric_not_found", message="показателя нет в отчёте", retryable=False)
RETRYABLE = typed_error_event(code="canonical_pipeline_incomplete", message="часть узлов упала", retryable=True)


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


@dataclass
class Journal:
    events: list[dict[str, Any]]

    async def __call__(self) -> AsyncIterator[dict[str, Any]]:
        for event in self.events:
            yield event


async def completion_of(events: list[dict[str, Any]]) -> CompletedEvent:
    run_id = ro.start_run(Journal(events), kind="chat", meta={})
    replay = [e async for e in ro.subscribe_versioned(run_id, kind="chat")]
    assert isinstance(replay[-1], CompletedEvent)
    return replay[-1]


@pytest.mark.parametrize("refusal", [REFUSAL, RETRYABLE], ids=["refusal", "retryable"])
async def test_an_answer_after_a_pipeline_refusal_closes_the_turn_as_succeeded(refusal: dict[str, Any]) -> None:
    completed = await completion_of([refusal, *answer_events("Показателя в отчёте нет.")])

    assert completed.status == "succeeded"


async def test_the_refusal_card_stays_in_the_journal_of_a_turn_that_got_an_answer() -> None:
    run_id = ro.start_run(Journal([RETRYABLE, *answer_events("Ответ.")]), kind="chat", meta={})

    replay = [e async for e in ro.subscribe_versioned(run_id, kind="chat")]

    card = next(i for i, e in enumerate(replay) if isinstance(e, ErrorEvent))
    text = next(i for i, e in enumerate(replay) if isinstance(e, LegacyCompatibilityEvent) and e.legacy_event["type"] == "text_delta")
    assert card < text


@pytest.mark.parametrize("refusal", [REFUSAL, RETRYABLE], ids=["refusal", "retryable"])
async def test_a_refusal_without_an_answer_still_closes_the_turn_as_the_old_chat_did(refusal: dict[str, Any]) -> None:
    completed = await completion_of([refusal, {"type": "done"}])

    assert completed.status == "failed"
