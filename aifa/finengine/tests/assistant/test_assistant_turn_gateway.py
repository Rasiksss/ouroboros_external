"""`turn_gateway`: кадры → события журнала хода (CONTRACTS.md §2.3–§2.4).

Модульные тесты на подставном сервере ouroboros (`fake_ouroboros.py`). Те же правила на настоящем
ouroboros — `aifa/tests/contract/test_turn_contract.py`.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fake_ouroboros import FakeOuroboros, assistant, echo
from turn_harness import NameRootOnDelivery, Subtasks, drain, gateway_of, request_of, shape
from websockets.asyncio.server import ServerConnection

from finengine.assistant.turn_gateway import SubtaskResult

ROOT = "r00t"
SUBTASK = "sub1"

# Тексты для пользователя закреплены дословно: правка текста — осознанная правка теста.
UNAVAILABLE = "Ассистент сейчас недоступен. Повторите вопрос через несколько минут."
REFUSED = "Ассистент недоступен для вашей учётной записи. Обратитесь к администратору."
REJECTED = "Ассистент не принял сообщение. Повторите вопрос."
UNCONFIGURED = (
    "Ассистент запросил подтверждение действия, которое в этой системе не предусмотрено. "
    "Сообщите администратору."
)


async def answers(connection: ServerConnection, server: FakeOuroboros) -> None:
    asked = await server.read_frame(connection)
    run_id = asked["client_message_id"]
    await server.send(
        connection,
        echo(run_id, asked["content"]),
        {"type": "typing", "action": "typing"},
        assistant(run_id, "💬 Смотрю данные.", task_id=ROOT, progress=True),
        assistant(run_id, "🗓️ Scheduled task sub1: проверить"),
        assistant(run_id, "Выручка выросла на 12%.", task_id=ROOT),
    )
    await server.read_until_closed(connection)


async def subtask_answers_first(connection: ServerConnection, server: FakeOuroboros) -> None:
    asked = await server.read_frame(connection)
    run_id = asked["client_message_id"]
    await server.send(
        connection,
        echo(run_id, asked["content"]),
        assistant(run_id, "Итог подзадачи.", task_id=SUBTASK),
        assistant(run_id, "⚠️ Error during processing: сбой", task_id="sub2"),
        assistant(run_id, "Чужой ход.", task_id="sub3", source="another-run"),
        assistant(run_id, "Основной ответ.", task_id=ROOT),
    )
    await server.read_until_closed(connection)


async def foreign_then_own_final(connection: ServerConnection, server: FakeOuroboros) -> None:
    asked = await server.read_frame(connection)
    run_id = asked["client_message_id"]
    await server.send(
        connection,
        assistant(run_id, "Ответ на другой вопрос.", task_id="other", source="another-run"),
        assistant(run_id, "Мой ответ.", task_id=ROOT),
    )
    await server.read_until_closed(connection)


async def asks_for_confirmation(connection: ServerConnection, server: FakeOuroboros) -> None:
    await server.read_frame(connection)
    await server.send(connection, {"type": "confirmation_request", "tool": "code_exec"})
    await server.read_until_closed(connection)


async def refuses_the_message(connection: ServerConnection, server: FakeOuroboros) -> None:
    await server.read_frame(connection)
    await server.send(connection, {"type": "error", "code": "message_too_long", "message": "x"})
    await server.read_until_closed(connection)


async def fails_to_persist(connection: ServerConnection, server: FakeOuroboros) -> None:
    await server.read_frame(connection)
    await server.send(connection, {"type": "error", "code": "message_persistence_failed"})
    await server.read_until_closed(connection)


async def answers_slowly_with_heartbeats(connection: ServerConnection, server: FakeOuroboros) -> None:
    asked = await server.read_frame(connection)
    run_id = asked["client_message_id"]
    for _ in range(8):
        await asyncio.sleep(0.1)
        await server.send(connection, {"type": "heartbeat"})
    await server.send(connection, assistant(run_id, "Долгий ответ.", task_id=ROOT))
    await server.read_until_closed(connection)


async def test_the_answer_comes_in_one_piece_and_everything_else_is_service() -> None:
    """§2.3: финал корня → `text_delta` (весь текст), `text_segment_end`, `done`; остальное — служебное."""
    async with FakeOuroboros(answers) as server:
        events = await drain(gateway_of(server), request_of())

    assert shape(events) == [
        "delivered",
        "progress",
        "notification",
        "text_delta",
        "text_segment_end",
        "done",
    ]
    assert events[1] == {"type": "ouro_service", "kind": "progress", "task_id": ROOT, "text": "💬 Смотрю данные."}
    assert events[3] == {"type": "text_delta", "text": "Выручка выросла на 12%."}
    assert server.received == [
        {"type": "chat", "content": "вопрос", "client_message_id": "run-1", "sender_session_id": "aifa"}
    ]
    assert server.handshakes[0]["x-ouroboros-owner-id"] == "7"
    assert server.handshakes[0]["x-ouroboros-chat-id"] == "project-1"


async def test_a_subtask_answer_before_the_final_goes_to_the_extension_point_not_to_the_screen() -> None:
    """ADR-0041 п. 3: корень назван (`root_known`) — итог подзадачи не финал; `failed` по «⚠️ Error»."""
    subtasks = Subtasks()
    async with FakeOuroboros(subtask_answers_first) as server:
        gateway = gateway_of(server, subtasks)
        request = request_of()
        events = await drain(gateway, request, NameRootOnDelivery(gateway, "run-1", ROOT))

    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["Основной ответ."]
    assert subtasks.results == [
        SubtaskResult("run-1", SUBTASK, "Итог подзадачи.", False),
        SubtaskResult("run-1", "sub2", "⚠️ Error during processing: сбой", True),
    ], "кадр чужого хода (другой source_message_id) не учитывается (ADR-0044 п. 9)"
    kinds = shape(events)
    assert kinds.count("subtask_result") == 2
    assert kinds.index("subtask_result") < kinds.index("text_delta")


@dataclass
class Roots:
    told: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, run_id: str, task_id: str) -> None:
        self.told.append((run_id, task_id))


async def test_the_root_of_a_finished_turn_is_told_once_even_when_a_subtask_answered_first() -> None:
    """§2.6: по этому корню история отбирает финал хода — не по порядку записей (ревью шага 11, В2)."""
    roots = Roots()
    async with FakeOuroboros(subtask_answers_first) as server:
        gateway = gateway_of(server, Subtasks())
        watch = NameRootOnDelivery(gateway, "run-1", ROOT)
        async for event in gateway.events(request_of(), on_root=roots):
            watch(event)

    assert roots.told == [("run-1", ROOT)]


async def test_a_turn_that_never_got_a_root_tells_nothing() -> None:
    roots = Roots()
    async with FakeOuroboros(answers, refuse_handshake=True) as server:
        async for _event in gateway_of(server).events(request_of(), on_root=roots):
            pass

    assert roots.told == []


async def test_without_the_root_named_the_first_task_frame_is_the_final() -> None:
    """§2.4: корень не назван — первым финалом становится кадр подзадачи (до шага 09 так и работает)."""
    async with FakeOuroboros(subtask_answers_first) as server:
        events = await drain(gateway_of(server), request_of())

    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["Итог подзадачи."]


async def test_a_frame_of_another_turn_in_the_same_chat_does_not_end_this_turn() -> None:
    """ADR-0021 п. 4: кадры другого хода (`source_message_id` другой) пропускаются."""
    async with FakeOuroboros(foreign_then_own_final) as server:
        events = await drain(gateway_of(server), request_of())

    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["Мой ответ."]


async def test_the_root_can_be_named_only_for_a_live_turn_and_only_once() -> None:
    async with FakeOuroboros(answers) as server:
        gateway = gateway_of(server)
        assert gateway.root_known("no-such-run", ROOT) is False

        seen: list[bool] = []
        async for event in gateway.events(request_of()):
            if event.get("kind") == "delivered":
                seen += [
                    gateway.root_known("run-1", ROOT),
                    gateway.root_known("run-1", ROOT),
                    gateway.root_known("run-1", "another-root"),
                ]
    assert seen == [True, True, False]
    assert gateway.root_known("run-1", ROOT) is False, "после хода он не живой"


async def test_a_confirmation_request_is_a_configuration_violation_and_the_task_is_stopped() -> None:
    """§2.3: подтверждение выключено — запрос это `error` хода и запись о нарушении; задача снимается."""
    async with FakeOuroboros(asks_for_confirmation) as server:
        events = await drain(gateway_of(server), request_of())
        await server.wait_received(2)

    assert events == [
        {"type": "ouro_service", "kind": "config_violation", "tool": "code_exec"},
        {"type": "error", "message": UNCONFIGURED},
    ]
    assert server.received[1] == {"type": "command", "cmd": "/stop", "client_message_id": "run-1-stop"}


async def test_a_transport_error_is_an_error_of_the_turn_with_a_clear_text() -> None:
    """§2.3: `message_too_long` и прочие отказы транспорта — `error` хода, без подробностей ouroboros."""
    async with FakeOuroboros(refuses_the_message) as server:
        too_long = await drain(gateway_of(server), request_of())
    async with FakeOuroboros(fails_to_persist) as server:
        other = await drain(gateway_of(server), request_of())

    assert too_long[-1] == {
        "type": "error",
        "message": "Сообщение слишком длинное: 6 знаков при допустимых 100. Сократите вопрос.",
    }
    assert other == [
        {"type": "ouro_service", "kind": "transport_error", "code": "message_persistence_failed"},
        {"type": "error", "message": REJECTED},
    ]


async def test_a_question_over_the_limit_never_reaches_ouroboros() -> None:
    async with FakeOuroboros(answers) as server:
        events = await drain(gateway_of(server), request_of("я" * 101))

    assert events == [
        {
            "type": "error",
            "message": "Сообщение слишком длинное: 101 знаков при допустимых 100. Сократите вопрос.",
        }
    ]
    assert server.received == []


async def test_a_refused_handshake_and_a_down_ouroboros_end_the_turn_with_their_own_texts() -> None:
    """§2.1: отказ рукопожатия и недоступность — разные тексты, оба без подробностей ouroboros."""
    async with FakeOuroboros(answers, refuse_handshake=True) as refusing:
        refused = await drain(gateway_of(refusing), request_of())
    async with FakeOuroboros(answers) as down:
        down.stop_accepting()
        unavailable = await drain(gateway_of(down), request_of())

    assert refused == [{"type": "error", "message": REFUSED}]
    assert unavailable == [{"type": "error", "message": UNAVAILABLE}]


async def test_heartbeats_keep_a_quiet_connection_alive() -> None:
    """§2.3: биение продлевает срок соединения — тишина между кадрами дольше `silence_sec` не обрыв."""
    async with FakeOuroboros(answers_slowly_with_heartbeats) as server:
        events = await drain(gateway_of(server, silence_sec=0.3), request_of())

    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["Долгий ответ."]
    assert server.accepted == 1, "переподключения не было"


ATTACHMENT = {
    "id": "f1", "kind": "file", "filename": "итоги.xlsx", "size": 6946, "status": "ready", "expires_at": "",
    "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "url": "api/chat/files/f1/%D0%B8%D1%82%D0%BE%D0%B3%D0%B8.xlsx",
}


async def sends_a_file_then_answers(connection: ServerConnection, server: FakeOuroboros) -> None:
    """Кадр файла ouroboros (`transport/browser/ws.py::_build_media_frame`): без `task_id` и `transport`."""
    asked = await server.read_frame(connection)
    run_id = asked["client_message_id"]
    await server.send(
        connection,
        {"type": "chat", "role": "assistant", "content": "", "attachments": [ATTACHMENT], "ts": "t", "source": "s"},
        assistant(run_id, "Файл приложен.", task_id=ROOT),
    )
    await server.read_until_closed(connection)


async def test_a_file_the_agent_sends_reaches_the_journal_with_its_catalog_id() -> None:
    """Dev, 21.10.2026: `file_read` агента пришёл уведомлением с `attachments`, вложение терялось."""
    async with FakeOuroboros(sends_a_file_then_answers) as server:
        events = await drain(gateway_of(server), request_of())

    assert {"type": "ouro_service", "kind": "attachment", "catalog_file_id": "f1", "filename": "итоги.xlsx",
            "mime": ATTACHMENT["mime"], "size": 6946} in events
    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["Файл приложен."]
