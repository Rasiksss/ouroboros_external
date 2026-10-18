"""`/events` сообщает конец хода, упавшего или отменённого («Стоп»), конвертом `completed` (P45 а).

Служба: `_drive` при исключении и при отмене пишет в журнал `error` и закрывает прогон, а `done`
в журнал не попадает. Раньше `subscribe_versioned` превращал в `completed` только `done`, и
журнал `[…, error]` заканчивался без признака конца: экран считал ход открытым, переподключался
семь раз и сообщал, что «прогон продолжается на сервере».

`completed` дописывается только когда прогон ЗАКРЫТ по общему состоянию. Журнал, у которого
`error` стоит последним, но прогон жив или его состояние не прочитано (отказ кэша у чужой
реплики), конца не получает: `error` бывает и посреди хода (`orchestrator/agent.py`), за ним идёт
продолжение.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from finengine.auth.db import User
from finengine.contracts.transport import CompletedEvent, ErrorEvent, LegacyCompatibilityEvent
from finengine.skills import run_orchestrator as ro
from finengine.store import bus as B
from finengine.store import cache as C


@pytest.fixture(autouse=True)
def чистая_координация(tmp_path, monkeypatch):
    from finengine.config import settings

    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(settings, "redis_url", "")
    B.reset_bus()
    C._cached = None
    yield
    B.reset_bus()
    C._cached = None
    ro._runs.clear()


def _types(events) -> list[str]:
    return [event.event_type for event in events]


async def _read(run_id: str, **kwargs) -> list:
    return [event async for event in ro.subscribe_versioned(run_id, kind="chat", **kwargs)]


async def _failing_run() -> str:
    async def runner():
        yield {"type": "text_delta", "text": "начало"}
        raise RuntimeError("сбой модели")

    run_id = ro.start_run(runner, kind="chat", meta={})
    await asyncio.sleep(0.05)
    return run_id


@pytest.mark.asyncio
async def test_a_failed_turn_ends_with_completed_failed() -> None:
    run_id = await _failing_run()

    events = await _read(run_id)

    assert _types(events) == ["legacy", "error", "completed"]
    assert isinstance(events[1], ErrorEvent)
    assert events[2].status == "failed"
    assert events[2].sequence == 3


@pytest.mark.asyncio
async def test_a_turn_stopped_by_the_user_ends_with_completed_cancelled() -> None:
    started = asyncio.Event()

    async def runner():
        yield {"type": "text_delta", "text": "думаю"}
        started.set()
        await asyncio.sleep(30)
        yield {"type": "done"}

    run_id = ro.start_run(runner, kind="chat", meta={})
    await started.wait()
    assert ro.cancel_run(run_id) is True
    await asyncio.sleep(0.05)

    events = await _read(run_id)

    assert _types(events) == ["legacy", "error", "completed"]
    assert events[2].status == "cancelled"


@pytest.mark.asyncio
async def test_a_live_tail_of_a_failing_turn_also_ends_with_completed() -> None:
    release = asyncio.Event()

    async def runner():
        yield {"type": "text_delta", "text": "начало"}
        await release.wait()
        raise RuntimeError("сбой модели")

    run_id = ro.start_run(runner, kind="chat", meta={})
    await asyncio.sleep(0.05)
    tail = asyncio.create_task(_read(run_id))
    await asyncio.sleep(0.05)
    release.set()

    events = await asyncio.wait_for(tail, timeout=5)

    assert _types(events) == ["legacy", "error", "completed"]
    assert events[2].status == "failed"


@pytest.mark.asyncio
async def test_a_reconnect_after_the_error_still_gets_the_end_exactly_once() -> None:
    run_id = await _failing_run()

    after_text = await _read(run_id, after_sequence=1)
    after_error = await _read(run_id, after_sequence=2)
    after_end = await _read(run_id, after_sequence=3)

    assert _types(after_text) == ["error", "completed"]
    assert _types(after_error) == ["completed"]
    assert after_end == []


@pytest.mark.asyncio
async def test_a_turn_that_reaches_done_gets_one_completed_not_two() -> None:
    async def runner():
        yield {"type": "error", "message": "промежуточная"}
        yield {"type": "text_delta", "text": "продолжение"}
        yield {"type": "done"}

    run_id = ro.start_run(runner, kind="chat", meta={})
    await asyncio.sleep(0.05)

    events = await _read(run_id)

    assert _types(events) == ["error", "legacy", "completed"]
    assert sum(isinstance(event, CompletedEvent) for event in events) == 1
    assert isinstance(events[1], LegacyCompatibilityEvent)


@pytest.mark.asyncio
async def test_an_error_in_the_middle_of_a_live_run_is_not_an_end(monkeypatch) -> None:
    """Поток чужой реплики закрылся при отказе кэша: прогон может быть жив, `error` — промежуточный."""
    async def closed_early(*_args, **_kwargs):
        yield {"type": "text_delta", "text": "начало"}
        yield {"type": "error", "message": "промежуточная"}

    monkeypatch.setattr(ro, "subscribe", closed_early)
    monkeypatch.setattr(ro, "состояние_прогона", lambda _run_id: None)

    events = await _read("foreign-run")

    assert _types(events) == ["legacy", "error"]


@pytest.mark.asyncio
async def test_an_error_in_a_run_that_is_still_open_is_not_an_end(monkeypatch) -> None:
    async def closed_early(*_args, **_kwargs):
        yield {"type": "error", "message": "промежуточная"}

    monkeypatch.setattr(ro, "subscribe", closed_early)
    monkeypatch.setattr(ro, "состояние_прогона", lambda _run_id: {"finished": False})

    assert _types(await _read("foreign-run")) == ["error"]


@pytest.mark.asyncio
async def test_a_finished_run_of_another_replica_ends_with_completed(monkeypatch) -> None:
    async def journal_only(*_args, **_kwargs):
        yield {"type": "error", "message": "run cancelled"}

    monkeypatch.setattr(ro, "subscribe", journal_only)
    monkeypatch.setattr(ro, "состояние_прогона", lambda _run_id: {"finished": True})

    events = await _read("foreign-run")

    assert _types(events) == ["error", "completed"]
    assert events[1].status == "cancelled"


@pytest.mark.asyncio
async def test_a_run_that_is_not_found_gets_no_end(monkeypatch) -> None:
    """«Запуск не найден» — не ход, который закончился: конца у него нет."""
    monkeypatch.setattr(ro, "состояние_прогона", lambda _run_id: None)

    events = await _read("no-such-run")

    assert _types(events) == ["error"]


@pytest.mark.asyncio
async def test_the_wire_frames_of_a_failed_turn_end_with_completed() -> None:
    """Через настоящую ручку: то, что читает браузер."""
    from finengine.api import main as api_main

    run_id = await _failing_run()
    user = User(id=1, username="test", password_hash="x", is_admin=False)

    response = await api_main.chat_run_events(run_id, request=_Request(), user=user)
    frames = [chunk.decode() async for chunk in response.body_iterator]
    payloads = [
        json.loads(frame.split("data: ", 1)[1]) for frame in frames if "data: " in frame
    ]

    assert [p["event_type"] for p in payloads if p["event_type"] != "heartbeat"] == [
        "legacy", "error", "completed",
    ]
    assert payloads[-1]["status"] == "failed"


class _Request:
    headers: dict[str, str] = {}
    query_params: dict[str, str] = {}


@pytest.mark.asyncio
async def test_an_error_after_done_does_not_add_a_second_completed() -> None:
    """Исключение после `done` (например, в `finally` раннера чата): итог хода уже выдан."""

    async def runner():
        yield {"type": "text_delta", "text": "ответ"}
        yield {"type": "done"}
        raise RuntimeError("сбой после ответа")

    run_id = ro.start_run(runner, kind="chat", meta={})
    await asyncio.sleep(0.05)

    events = await _read(run_id)

    assert _types(events) == ["legacy", "completed", "error"]
    assert events[1].status == "succeeded"
    assert [e.event_type for e in await _read(run_id, after_sequence=2)] == ["error"]


@pytest.mark.asyncio
async def test_a_typed_error_without_done_ends_with_completed_failed() -> None:
    """Терминальный `typed_error` (журнал кончился на нём) закрывается так же, как `error`."""
    failure = {
        "code": "dynamic_run_failed",
        "message": "сбой",
        "retryable": False,
        "node_key": None,
        "details": {},
    }

    async def runner():
        yield {"type": "text_delta", "text": "начало"}
        yield {"type": "typed_error", "error": failure, "partial_results": []}

    run_id = ro.start_run(runner, kind="chat", meta={})
    await asyncio.sleep(0.05)

    events = await _read(run_id)

    assert _types(events) == ["legacy", "error", "completed"]
    assert events[2].status == "failed"
    assert events[2].sequence == 3
