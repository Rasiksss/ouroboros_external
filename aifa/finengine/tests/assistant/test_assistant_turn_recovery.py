"""`turn_gateway`: обрыв, история, крайний срок, `session_lost` (CONTRACTS.md §2.7, ADR-0021 п. 5).

Модульные тесты на подставном сервере ouroboros; тексты отказов закреплены дословно.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fake_ouroboros import FakeOuroboros, assistant, echo, history_record
from turn_harness import drain, gateway_of, request_of, shape
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from finengine.assistant.history import HistoryPage, HistoryRecord, find_final

LOST = "Связь с ассистентом прервалась, и ответ получить не удалось. Задайте вопрос ещё раз."
TIMED_OUT = "Ассистент не ответил за отведённое время. Задайте вопрос ещё раз, при необходимости короче."
ROOT = "r00t"
ANSWER = "Ответ из истории."


def answered_history(run_id: str = "run-1") -> dict[str, Any]:
    return {
        "messages": [
            history_record("user", "вопрос", run_id),
            history_record(
                "assistant", "💬 думаю", run_id, task_id=ROOT, progress=True, ts="2026-10-18T10:00:01+00:00"
            ),
            history_record("assistant", ANSWER, run_id, task_id=ROOT, ts="2026-10-18T10:00:02+00:00"),
        ]
    }


async def drops_after_the_echo(connection: ServerConnection, server: FakeOuroboros) -> None:
    if server.accepted > 1:
        await server.read_until_closed(connection)
        return
    asked = await server.read_frame(connection)
    await server.send(connection, echo(asked["client_message_id"], asked["content"]))


async def drops_then_answers_on_the_second_connection(
    connection: ServerConnection, server: FakeOuroboros
) -> None:
    if server.accepted == 1:
        await drops_after_the_echo(connection, server)
        return
    await asyncio.sleep(0.1)
    await server.send(connection, assistant("run-1", "Ответ по новому соединению.", task_id=ROOT))
    await server.read_until_closed(connection)


async def goes_down_after_the_echo(connection: ServerConnection, server: FakeOuroboros) -> None:
    await drops_after_the_echo(connection, server)
    server.stop_accepting()


async def goes_quiet(connection: ServerConnection, server: FakeOuroboros) -> None:
    if server.accepted > 1:
        await server.read_until_closed(connection)
        return
    await server.read_frame(connection)
    await asyncio.sleep(5.0)


async def beats_forever(connection: ServerConnection, server: FakeOuroboros) -> None:
    await server.read_frame(connection)
    reader = asyncio.create_task(server.read_until_closed(connection))
    try:
        while not reader.done():
            await asyncio.sleep(0.05)
            await server.send(connection, {"type": "heartbeat"})
    except ConnectionClosed:
        return
    finally:
        reader.cancel()


async def test_after_a_drop_the_answer_is_found_in_the_history() -> None:
    """§2.7: соединение оборвалось — переподключение и поиск финала в истории по `client_message_id`."""
    async with FakeOuroboros(drops_after_the_echo, history=[answered_history()]) as server:
        events = await drain(gateway_of(server), request_of())

    assert shape(events) == ["delivered", "text_delta", "text_segment_end", "done"]
    assert events[1]["text"] == ANSWER
    assert server.accepted == 2, "переподключились с теми же заголовками"
    identity = ("x-ouroboros-owner-id", "x-ouroboros-chat-id", "x-trace-id")
    assert [server.handshakes[1][name] for name in identity] == [
        server.handshakes[0][name] for name in identity
    ]
    assert server.history_calls[0][0]["x-ouroboros-chat-id"] == "project-1"


async def test_after_a_drop_the_new_connection_goes_on_when_the_history_has_no_answer_yet() -> None:
    async with FakeOuroboros(drops_then_answers_on_the_second_connection) as server:
        events = await drain(gateway_of(server), request_of())

    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["Ответ по новому соединению."]
    assert server.accepted == 2 and len(server.history_calls) == 1


async def test_while_ouroboros_is_down_the_history_is_polled_until_the_answer_appears() -> None:
    """§2.7: переподключиться не удалось — опрос истории; сбой самой истории опрос не прерывает."""
    async with FakeOuroboros(goes_down_after_the_echo, history=[500, answered_history()]) as server:
        events = await drain(gateway_of(server), request_of())

    assert [e["text"] for e in events if e["type"] == "text_delta"] == [ANSWER]
    assert len(server.history_calls) == 2


async def test_an_answer_that_never_appears_ends_the_turn_with_session_lost() -> None:
    """§2.7: не нашли до крайнего срока — `session_lost` и `done`, как у журнала после перезапуска."""
    async with FakeOuroboros(goes_down_after_the_echo) as server:
        events = await drain(gateway_of(server, deadline_sec=0.6), request_of())

    assert events[1:] == [
        {"type": "session_lost", "message": LOST},
        {"type": "done"},
    ]


async def test_a_connection_that_goes_silent_is_treated_as_a_drop() -> None:
    """Тишина дольше `silence_sec` — обрыв: переподключение и история."""
    async with FakeOuroboros(goes_quiet, history=[answered_history()]) as server:
        events = await drain(gateway_of(server, silence_sec=0.2), request_of())

    assert [e["text"] for e in events if e["type"] == "text_delta"] == [ANSWER]
    assert server.accepted == 2


async def test_at_the_deadline_a_live_but_silent_ouroboros_gets_an_error_and_a_stop() -> None:
    """ADR-0021 п. 1: крайний срок хода — `error`, а задача на стороне ouroboros снимается `/stop`."""
    async with FakeOuroboros(beats_forever) as server:
        events = await drain(gateway_of(server, deadline_sec=0.5, silence_sec=0.3), request_of())
        await server.wait_received(2)

    assert events == [{"type": "error", "message": TIMED_OUT}]
    assert server.received[1] == {"type": "command", "cmd": "/stop", "client_message_id": "run-1-stop"}


async def test_at_the_deadline_an_answer_that_is_in_the_history_still_counts() -> None:
    async with FakeOuroboros(beats_forever, history=[answered_history()]) as server:
        events = await drain(gateway_of(server, deadline_sec=0.4, silence_sec=0.3), request_of())

    assert shape(events) == ["text_delta", "text_segment_end", "done"]
    assert events[0]["text"] == ANSWER


@dataclass
class Pages:
    """Страницы истории по курсору: пустая строка — самая свежая."""

    by_cursor: dict[str, HistoryPage]
    asked: list[str] = field(default_factory=list)

    async def __call__(self, before_ts: str) -> HistoryPage:
        self.asked.append(before_ts)
        return self.by_cursor[before_ts]


def record(role: str, run_id: str, task_id: str = "", *, text: str = "т", progress: bool = False) -> HistoryRecord:
    return HistoryRecord(role, text, progress, run_id, task_id, "2026-10-18T10:00:00+00:00")


async def test_the_final_is_looked_for_on_older_pages_until_the_question_is_met() -> None:
    """§2.6: страницы идут от свежих к старым; реплика хода — граница поиска."""
    newer = HistoryPage((record("assistant", "run-2", "x"),), "c1")
    older = HistoryPage(
        (record("user", "run-1"), record("assistant", "run-1", ROOT, text="Финал")), "c2"
    )
    pages = Pages({"": newer, "c1": older})

    found = await find_final(pages, run_id="run-1", root_task_id=ROOT)

    assert found is not None and found.text == "Финал"
    assert pages.asked == ["", "c1"]


async def test_the_search_stops_at_the_question_of_the_turn() -> None:
    """Реплика хода найдена, ответа нет — старее искать нечего."""
    newest = HistoryPage((record("user", "run-1"),), "c1")
    pages = Pages({"": newest})

    assert await find_final(pages, run_id="run-1", root_task_id="") is None
    assert pages.asked == [""]


async def test_progress_and_other_tasks_are_not_the_final_once_the_root_is_known() -> None:
    page = HistoryPage(
        (
            record("user", "run-1"),
            record("assistant", "run-1", ROOT, progress=True, text="💬"),
            record("assistant", "run-1", "sub1", text="Итог подзадачи"),
            record("assistant", "run-1", "", text="Scheduled task"),
        ),
        "",
    )

    assert await find_final(Pages({"": page}), run_id="run-1", root_task_id=ROOT) is None
    unnamed = await find_final(Pages({"": page}), run_id="run-1", root_task_id="")
    assert unnamed is not None and unnamed.text == "Итог подзадачи"


async def test_the_search_does_not_walk_the_whole_history() -> None:
    endless = {str(n): HistoryPage((record("assistant", "run-9", "x"),), str(n + 1)) for n in range(20)}
    endless[""] = HistoryPage((record("assistant", "run-9", "x"),), "0")
    pages = Pages(endless)

    assert await find_final(pages, run_id="run-1", root_task_id=ROOT) is None
    assert len(pages.asked) == 5
