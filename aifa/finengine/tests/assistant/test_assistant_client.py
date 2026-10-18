"""`ouro_client`: заголовки рукопожатия, кадр по §2.2, отказы, история (CONTRACTS.md §2.1, §2.2, §2.6).

Модульные тесты на подставном сервере; тот же клиент на настоящем ouroboros — `aifa/tests/contract`.
"""
from __future__ import annotations

import pytest
from fake_ouroboros import FakeOuroboros, history_record
from websockets.asyncio.server import ServerConnection

from finengine.assistant.ouro_client import (
    ChatScope,
    ForbiddenFrame,
    HandshakeRefused,
    LinkLost,
    MessageTooLong,
    OuroClient,
    OuroUnavailable,
)

SCOPE = ChatScope("7", "project-1")


async def listen(connection: ServerConnection, server: FakeOuroboros) -> None:
    await server.read_until_closed(connection)


async def hang_up(connection: ServerConnection, server: FakeOuroboros) -> None:
    await connection.close()


def client_of(server: FakeOuroboros, *, max_chars: int = 100) -> OuroClient:
    return OuroClient(server.url, connect_timeout_sec=2.0, max_message_chars=max_chars)


async def test_the_handshake_carries_the_owner_chat_and_trace_but_no_agent() -> None:
    """C2.1 / F12: заголовки владельца ставит клиент; `X-Ouroboros-Agent-Id` не передаётся (ADR-0002)."""
    async with FakeOuroboros(listen) as server:
        link = await client_of(server).open_turn(SCOPE, "trace-1")
        await link.close()

    (headers,) = server.handshakes
    assert headers["x-ouroboros-owner-id"] == "7"
    assert headers["x-ouroboros-chat-id"] == "project-1"
    assert headers["x-trace-id"] == "trace-1"
    assert "x-ouroboros-agent-id" not in headers


async def test_the_question_goes_as_the_frame_of_the_contract_and_nothing_else() -> None:
    """§2.2: `chat` с текстом дословно, `client_message_id == run_id`, `sender_session_id`."""
    async with FakeOuroboros(listen) as server:
        link = await client_of(server).open_turn(SCOPE, "trace-1")
        await link.send_chat("  Какая выручка?\n", "run-1")
        received = await server.wait_received(1)
        await link.close()

    assert received == [
        {
            "type": "chat",
            "content": "  Какая выручка?\n",
            "client_message_id": "run-1",
            "sender_session_id": "aifa",
        }
    ]


async def test_stop_is_the_command_frame_with_its_own_message_id() -> None:
    """§2.7: `/stop` — кадр `command`; свой `client_message_id`, чтобы ответ на него не приняли за кадр хода."""
    async with FakeOuroboros(listen) as server:
        link = await client_of(server).open_turn(SCOPE, "trace-1")
        await link.send_stop("run-1")
        received = await server.wait_received(1)
        await link.close()

    assert received == [{"type": "command", "cmd": "/stop", "client_message_id": "run-1-stop"}]


@pytest.mark.parametrize(
    "extra",
    [
        {"attachment_ids": ["f1"]},
        {"attachment_ids": []},
        {"detached": True},
        {"detached": False},
        {"note": "добавка"},
    ],
)
async def test_a_frame_with_fields_outside_the_contract_is_never_sent(extra: dict[str, object]) -> None:
    """F6 (во время исполнения): вложения, `detached` и любые лишние ключи — исключение до отправки."""
    async with FakeOuroboros(listen) as server:
        link = await client_of(server).open_turn(SCOPE, "trace-1")
        with pytest.raises(ForbiddenFrame):
            await link.send_frame(
                {"type": "chat", "content": "текст", "client_message_id": "run-1", **extra}
            )
        await link.send_chat("после", "run-2")
        received = await server.wait_received(1)
        await link.close()

    assert [frame["content"] for frame in received] == ["после"]


async def test_a_command_frame_may_not_carry_extra_fields_either() -> None:
    async with FakeOuroboros(listen) as server:
        link = await client_of(server).open_turn(SCOPE, "trace-1")
        with pytest.raises(ForbiddenFrame):
            await link.send_frame({"type": "command", "cmd": "/stop", "attachment_ids": ["f1"]})
        await link.close()

    assert server.received == []


async def test_a_message_over_the_limit_is_refused_before_it_is_sent() -> None:
    """§2.2: длина больше предела — отказ службы до отправки; считается текст без крайних пробелов."""
    async with FakeOuroboros(listen) as server:
        link = await client_of(server, max_chars=10).open_turn(SCOPE, "trace-1")
        await link.send_chat(" " + "я" * 10 + " ", "run-1")
        with pytest.raises(MessageTooLong) as refused:
            await link.send_chat("я" * 11, "run-2")
        await server.wait_received(1)
        await link.close()

    assert (refused.value.actual, refused.value.limit) == (11, 10)
    assert len(server.received) == 1


async def test_a_closed_handshake_is_a_refusal_and_a_down_server_is_unavailable() -> None:
    """§2.1: 403 до приёма — личность не принята; 503, закрытый порт — недоступен."""
    async with FakeOuroboros(listen, refuse_handshake=True) as refusing:
        with pytest.raises(HandshakeRefused):
            await client_of(refusing).open_turn(SCOPE, "trace-1")
    async with FakeOuroboros(listen) as down:
        down.stop_accepting()
        with pytest.raises(OuroUnavailable):
            await client_of(down).open_turn(SCOPE, "trace-1")
        dead_url = down.url
    with pytest.raises(OuroUnavailable):
        await OuroClient(dead_url, connect_timeout_sec=1.0, max_message_chars=10).open_turn(
            SCOPE, "trace-1"
        )


async def test_silence_is_a_timeout_and_a_closed_connection_is_lost() -> None:
    async with FakeOuroboros(listen) as server:
        link = await client_of(server).open_turn(SCOPE, "trace-1")
        with pytest.raises(TimeoutError):
            await link.recv(0.1)
        await link.close()
    async with FakeOuroboros(hang_up) as closing:
        link = await client_of(closing).open_turn(SCOPE, "trace-1")
        with pytest.raises(LinkLost):
            await link.recv(2.0)


async def test_the_history_page_is_read_with_the_owner_headers_and_the_cursor() -> None:
    """§2.6: `GET /api/chat/history?limit=&before_ts=` с заголовками владельца и чата."""
    page = {
        "messages": [
            history_record("user", "вопрос", "run-1"),
            history_record("assistant", "ответ", "run-1", task_id="r00t"),
        ],
        "has_more": True,
        "next_before_ts": "2026-10-18T09:00:00+00:00",
    }
    async with FakeOuroboros(listen, history=[page, {"messages": []}]) as server:
        client = client_of(server)
        first = await client.history(SCOPE, trace_id="trace-1")
        second = await client.history(SCOPE, before_ts=first.next_before_ts)

    assert [(r.role, r.text, r.client_message_id, r.task_id) for r in first.records] == [
        ("user", "вопрос", "run-1", ""),
        ("assistant", "ответ", "run-1", "r00t"),
    ]
    assert first.next_before_ts == "2026-10-18T09:00:00+00:00"
    assert second.records == () and second.next_before_ts == ""
    (headers, params), (_, older_params) = server.history_calls
    assert headers["x-ouroboros-owner-id"] == "7" and headers["x-ouroboros-chat-id"] == "project-1"
    assert headers["x-trace-id"] == "trace-1"
    assert params == {"limit": "50"}
    assert older_params["before_ts"].startswith("2026-10-18T09")


async def test_a_history_that_fails_is_unavailable() -> None:
    async with FakeOuroboros(listen, history=[500]) as server:
        with pytest.raises(OuroUnavailable):
            await client_of(server).history(SCOPE)
