"""Контрактные тесты C2.1–C2.8 (CONTRACTS.md §2): `ouro_client` и `turn_gateway` на живом ouroboros.

Профиль `contract`, подставная модель. C2.3–C2.4 закрывают P1: финальный кадр корня несёт `task_id`,
уведомление подзадачи — нет, ответ подзадачи приходит в чат со своим `task_id` (ADR-0021).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import pytest
from finengine.assistant import messages
from finengine.assistant.frames import FrameKind
from finengine.assistant.ouro_client import (
    CHAT_HEADER,
    OWNER_HEADER,
    ChatScope,
    ForbiddenFrame,
    HandshakeRefused,
)
from finengine.assistant.turn_gateway import TurnRequest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from stand import SCENARIOS, ContractStand
from stub_model.client import StubModelAdmin
from turn_support import (
    IsKind,
    client_of,
    collect,
    frames_until,
    gateway_of,
    new_chat,
    new_request,
    scope_of,
    screen,
    service,
)

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

AGENT_HEADER = "X-Ouroboros-Agent-Id"
HELLO_ANSWER = "Здравствуйте! Это подставная модель."
ROOT_ANSWER = "Подзадача запущена."
SUBTASK_ANSWER = "Итог подзадачи."


def history_status(stand: ContractStand, request: TurnRequest, agent_id: str) -> tuple[int, str]:
    headers = {OWNER_HEADER: request.owner_id, CHAT_HEADER: request.chat_id, AGENT_HEADER: agent_id}
    response = httpx.get(stand.ouro_http_url + "/api/chat/history", headers=headers)
    return response.status_code, response.text


async def test_c2_1_a_handshake_without_the_owner_is_closed(stand: ContractStand) -> None:
    """C2.1: рукопожатие без `X-Ouroboros-Owner-Id` закрывается — и сырое, и клиентом службы."""
    with pytest.raises(InvalidStatus) as raw:
        async with connect(stand.ouro_ws_url, additional_headers={CHAT_HEADER: new_chat()}):
            pass
    assert raw.value.response.status_code == 403

    with pytest.raises(HandshakeRefused):
        await client_of(stand).open_turn(ChatScope("", new_chat()), str(uuid.uuid4()))


async def test_c2_1_a_turn_without_an_agent_header_runs_as_the_main_agent(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """C2.1: клиент службы агента не называет, ход живёт в `main`: ту же беседу видит `main`."""
    stub.use_scenario(SCENARIOS / "hello.json")
    request = new_request("привет")

    await collect(gateway_of(stand), request)

    status, body = history_status(stand, request, "main")
    assert status == 200 and request.run_id in body
    assert history_status(stand, request, "another-agent")[0] == 403


async def test_c2_2_the_echo_carries_the_run_id(stand: ContractStand, stub: StubModelAdmin) -> None:
    """C2.2: эхо реплики приходит с `client_message_id == run_id`; служба узнаёт в нём доставку."""
    stub.use_scenario(SCENARIOS / "hello.json")
    request = new_request("привет")

    events = await collect(gateway_of(stand), request)

    assert len(service(events, "delivered")) == 1
    raw = new_request("привет")
    link = await client_of(stand).open_turn(scope_of(raw), raw.trace_id)
    try:
        await link.send_chat(raw.text, raw.run_id)
        frames = await frames_until(link, raw, IsKind(FrameKind.FINAL))
    finally:
        await link.close()
    echo = next(f for f in frames if f.get("role") == "user")
    assert echo["client_message_id"] == raw.run_id
    assert echo["content"] == "привет"


async def test_c2_3_the_final_of_the_root_names_its_task_and_the_run(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """C2.3: финал корня несёт `task_id` и `transport.source_message_id == run_id`."""
    stub.use_scenario(SCENARIOS / "hello.json")
    request = new_request("привет")

    events = await collect(gateway_of(stand), request)

    assert [e["type"] for e in screen(events)] == ["text_delta", "text_segment_end", "done"]
    assert screen(events)[0]["text"] == HELLO_ANSWER
    raw = new_request("привет")
    link = await client_of(stand).open_turn(scope_of(raw), raw.trace_id)
    try:
        await link.send_chat(raw.text, raw.run_id)
        frames = await frames_until(link, raw, IsKind(FrameKind.FINAL))
    finally:
        await link.close()
    final = frames[-1]
    assert final["task_id"] and final["is_progress"] is False
    assert final["transport"]["source_message_id"] == raw.run_id


async def test_c2_4_a_notification_and_a_subtask_answer_are_not_the_final(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """C2.4: «Scheduled task» (пустой `task_id`) и ответ подзадачи (свой) — не финал."""
    stub.use_scenario(SCENARIOS / "subtask.json")
    request = new_request("ход с подзадачей")
    link = await client_of(stand).open_turn(scope_of(request), request.trace_id)
    try:
        await link.send_chat(request.text, request.run_id)
        to_root = await frames_until(link, request, IsKind(FrameKind.FINAL))
        root = to_root[-1]
        assert root["content"] == ROOT_ANSWER
        notification = next(f for f in to_root if "Scheduled task" in str(f.get("content")))
        assert notification["task_id"] == ""
        to_subtask = await frames_until(
            link, request, IsKind(FrameKind.SUBTASK_RESULT), root_task_id=root["task_id"]
        )
    finally:
        await link.close()
    subtask = to_subtask[-1]
    assert subtask["content"] == SUBTASK_ANSWER
    assert subtask["task_id"] and subtask["task_id"] != root["task_id"]
    assert subtask["transport"]["source_message_id"] == request.run_id


async def test_c2_4_ouroboros_starts_a_subtask_while_its_root_still_works(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """C2.4 / P50 (ADR-0052): подзадача стартует, пока корень работает, — `aifa_wait` из корня
    возможен.

    Корень делает 50 кругов после `schedule_task`; запрос подзадачи уходит supervisor'у в конце
    вызова
    (`ouroboros/tools/service/orchestration.py`), а не с результатом run, поэтому первый запрос
    подзадачи к модели приходит раньше последнего круга корня. До правки ядра он шёл после него
    (ADR-0046): тест был пломбой «подзадача стартует после корня» и инвертирован этим шагом.
    """
    rules = [
        {"id": "title", "when": {"model": "stub-light"}, "reply": {"text": "Подзадача"}},
        {
            "id": "subtask-answers",
            "when": {"model": "stub-main", "user_contains": "Задача подзадачи"},
            "reply": {"text": SUBTASK_ANSWER},
        },
        {
            "id": "root-schedules-subtask",
            "when": {"model": "stub-main", "user_contains": "ход с ожиданием", "round": 1},
            "reply": {
                "tool_calls": [
                    {
                        "name": "schedule_task",
                        "arguments": {"description": "Задача подзадачи: ответь"},
                    }
                ]
            },
        },
        *(
            {
                "id": f"root-works-{n}",
                "when": {"model": "stub-main", "user_contains": "ход с ожиданием", "round": n},
                "reply": {"tool_calls": [{"name": "list_available_tools", "arguments": {}}]},
            }
            for n in range(2, 50)
        ),
        {
            "id": "root-answers",
            "when": {"model": "stub-main", "user_contains": "ход с ожиданием", "round": 50},
            "reply": {"text": "Ждал подзадачу."},
        },
    ]
    stub.use_scenario({"name": "subtask-after-root", "rules": rules})
    request = new_request("ход с ожиданием")
    link = await client_of(stand).open_turn(scope_of(request), request.trace_id)
    try:
        await link.send_chat(request.text, request.run_id)
        to_final = await frames_until(link, request, IsKind(FrameKind.FINAL))
        root_task = to_final[-1]["task_id"]
        await frames_until(link, request, IsKind(FrameKind.SUBTASK_RESULT), root_task_id=root_task)
    finally:
        await link.close()

    users = [str(e["user"]) for e in stub.journal() if e["model"] == "stub-main"]
    first_subtask = next(i for i, user in enumerate(users) if user.startswith("Задача подзадачи"))
    assert first_subtask < 50, "первый запрос подзадачи пришёл раньше последнего круга корня"
    assert any(not user.startswith("Задача подзадачи") for user in users[first_subtask:]), (
        "корень продолжил работать после старта подзадачи"
    )


async def test_c2_5_progress_is_marked_and_stays_off_the_screen(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """C2.5: прогресс несёт `is_progress: true`; в журнал идёт служебным, на экран — ответ."""
    stub.use_scenario(SCENARIOS / "progress.json")
    request = new_request("с пояснением")
    link = await client_of(stand).open_turn(scope_of(request), request.trace_id)
    try:
        await link.send_chat(request.text, request.run_id)
        frames = await frames_until(link, request, IsKind(FrameKind.FINAL))
    finally:
        await link.close()
    progress = [f for f in frames if f.get("is_progress")]
    assert [f["content"] for f in progress] == ["💬 Сейчас посмотрю инструменты."]

    events = await collect(gateway_of(stand), new_request("с пояснением"))

    assert [e["text"] for e in service(events, "progress")] == ["💬 Сейчас посмотрю инструменты."]
    assert [e["type"] for e in screen(events)] == ["text_delta", "text_segment_end", "done"]
    assert screen(events)[0]["text"] == "Готово."


async def test_c2_6_the_history_finds_the_turn_by_its_run_id_and_keeps_chats_apart(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """C2.6: записи хода — по `client_message_id`; чужой чат и чужой владелец их не видят."""
    stub.use_scenario(SCENARIOS / "hello.json")
    request = new_request("привет")
    await collect(gateway_of(stand), request)
    client = client_of(stand)

    own = await client.history(scope_of(request))
    mine = [r for r in own.records if r.client_message_id == request.run_id]
    assert [(r.role, r.text) for r in mine] == [("user", "привет"), ("assistant", HELLO_ANSWER)]
    assert mine[1].task_id and not mine[1].is_progress

    other_chat = await client.history(ChatScope(request.owner_id, new_chat()))
    other_owner = await client.history(ChatScope("someone-else", request.chat_id))
    assert [r for r in other_chat.records if r.client_message_id == request.run_id] == []
    assert [r for r in other_owner.records if r.client_message_id == request.run_id] == []


async def test_c2_7_stop_halts_the_root_and_its_subtasks(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """C2.7: `/stop` останавливает корень и подзадачу хода — запросы к модели перестают идти."""
    stub.use_scenario(SCENARIOS / "endless.json")
    request = new_request("ход бесконечный")
    link = await client_of(stand).open_turn(scope_of(request), request.trace_id)
    try:
        await link.send_chat(request.text, request.run_id)
        await _wait_until(AskedMoreThan(stub, "Задача бесконечная", 3))
        assert _asked(stub, "ход бесконечный") > 3
        before = len(stub.journal())
        await asyncio.sleep(0.5)
        assert len(stub.journal()) > before, "до «Стопа» ход идёт"
        await link.send_stop(request.run_id)
        await asyncio.sleep(3.0)
        halted = len(stub.journal())
        await asyncio.sleep(2.0)
    finally:
        await link.close()
    assert len(stub.journal()) == halted, "после /stop модель больше не спрашивают"
    assert halted < 400, "ход остановлен, а не дошёл до предела кругов"


async def test_c2_8_a_frame_with_attachments_is_never_sent(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """C2.8 (F6): кадр с вложениями клиент не отправляет, а соединение остаётся рабочим."""
    stub.use_scenario(SCENARIOS / "hello.json")
    request = new_request("привет")
    link = await client_of(stand).open_turn(scope_of(request), request.trace_id)
    try:
        for extra in ({"attachment_ids": ["f1"]}, {"detached": True}):
            with pytest.raises(ForbiddenFrame):
                await link.send_frame(
                    {"type": "chat", "content": "прикрепил", "client_message_id": "x", **extra}
                )
        await link.send_chat("привет", request.run_id)
        frames = await frames_until(link, request, IsKind(FrameKind.FINAL))
    finally:
        await link.close()
    assert frames[-1]["content"] == HELLO_ANSWER
    assert not stub.saw("прикрепил")


async def test_a_message_longer_than_ouroboros_accepts_is_refused_before_sending(
    stand: ContractStand, stub: StubModelAdmin
) -> None:
    """Длина больше предела: отказ службы до отправки, с текстом для пользователя (§2.2)."""
    stub.use_scenario(SCENARIOS / "hello.json")
    request = new_request("я" * 50_001)

    events = await collect(gateway_of(stand), request)

    assert events == [{"type": "error", "message": messages.too_long(50_001, 50_000)}]
    assert not stub.saw("яяяя"), "до модели текст не дошёл"


def _asked(stub: StubModelAdmin, user_prefix: str) -> int:
    return sum(1 for e in stub.journal() if str(e["user"]).startswith(user_prefix))


@dataclass(frozen=True)
class AskedMoreThan:
    stub: StubModelAdmin
    user_prefix: str
    count: int

    def __call__(self) -> bool:
        return _asked(self.stub, self.user_prefix) > self.count


async def _wait_until(condition: Callable[[], bool], timeout_sec: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.1)
    raise TimeoutError("условие не наступило")
