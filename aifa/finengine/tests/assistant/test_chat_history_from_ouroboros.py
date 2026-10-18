"""`GET /chat/history/{session_id}` отдаёт историю чата проекта из ouroboros (CONTRACTS.md §1, §2.6, ADR-0011).

Закрывает C1.3: формат прежний `{role, content}`; сообщения подзадач, уведомления и прогресс отфильтрованы;
чужой владелец — 404. Ouroboros — подставной сервер истории по сети (`fake_ouroboros.py`), ручка и клиент —
настоящие.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from fake_ouroboros import FakeOuroboros, history_record
from websockets.asyncio.server import ServerConnection

from finengine.assistant.ouro_client import OuroClient

USER = 7
SESSION = "5f3c2a10-7d3e-4c1b-9a55-0e2f6d1b8a01"
PROJECT = "proj-1"
RUN_1 = "11111111-1111-4111-8111-111111111111"
RUN_2 = "22222222-2222-4222-8222-222222222222"
ROOT_1, ROOT_2, SUB = "r00t0001", "r00t0002", "5ub00001"


async def nobody(connection: ServerConnection, server: FakeOuroboros) -> None:
    await server.read_until_closed(connection)


def turns() -> list[dict[str, Any]]:
    """Записи ouroboros по возрастанию времени: два хода, прогресс, итог подзадачи, уведомление."""
    return [
        history_record("user", "какая выручка?", RUN_1, ts="2026-10-18T10:00:01+00:00"),
        history_record("assistant", "⏳ думаю", RUN_1, task_id=ROOT_1, progress=True, ts="2026-10-18T10:00:02+00:00"),
        history_record("assistant", "Выручка 987 654.", RUN_1, task_id=ROOT_1, ts="2026-10-18T10:00:03+00:00"),
        history_record("assistant", "итог подзадачи", RUN_1, task_id=SUB, ts="2026-10-18T10:00:04+00:00"),
        history_record("assistant", "🗓️ Scheduled task finished", "note-1", ts="2026-10-18T10:00:05+00:00"),
        history_record("user", "а прибыль?", RUN_2, ts="2026-10-18T10:01:01+00:00"),
        history_record("assistant", "Прибыль 12 345.", RUN_2, task_id=ROOT_2, ts="2026-10-18T10:01:03+00:00"),
    ]


class Harness:
    def __init__(self, server: FakeOuroboros, client: httpx.AsyncClient, dialogs: dict[int, dict[str, Any]]):
        self.server, self.client, self.dialogs = server, client, dialogs


@pytest.fixture
async def history(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    from finengine.api import main as m
    from finengine.assistant import composition
    from finengine.auth.db import User
    from finengine.auth.dependencies import get_current_user

    dialogs: dict[int, dict[str, Any]] = {
        USER: {"projects": [{"id": PROJECT, "session_id": SESSION}], "folders": []}
    }
    monkeypatch.setattr(
        m, "get_dialog_store", lambda: SimpleNamespace(load=lambda uid: dialogs.get(uid, {"projects": []}))
    )
    current = {"id": USER}
    m.app.dependency_overrides[get_current_user] = lambda: User(id=current["id"], username="analyst")

    async def start(pages: list[Any]) -> Harness:
        server = FakeOuroboros(nobody, history=pages)
        await server.__aenter__()
        monkeypatch.setattr(
            composition, "ouro_client", lambda: OuroClient(server.url, connect_timeout_sec=1.0, max_message_chars=100)
        )
        transport = httpx.ASGITransport(app=m.app)
        client = httpx.AsyncClient(transport=transport, base_url="http://engine")
        harness = Harness(server, client, dialogs)
        harness.current = current  # type: ignore[attr-defined]
        opened.append(harness)
        return harness

    opened: list[Harness] = []
    yield start
    for harness in opened:
        await harness.client.aclose()
        await harness.server.__aexit__(None, None, None)
    m.app.dependency_overrides.pop(get_current_user, None)


async def test_history_is_the_users_replicas_and_the_final_of_each_root(history):
    """C1.3: формат прежний; прогресс, итог подзадачи и уведомление отфильтрованы."""
    h = await history([{"messages": turns()}])

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert got.status_code == 200, got.text
    assert got.json() == {
        "session_id": SESSION,
        "history": [
            {"role": "user", "content": "какая выручка?"},
            {"role": "assistant", "content": "Выручка 987 654."},
            {"role": "user", "content": "а прибыль?"},
            {"role": "assistant", "content": "Прибыль 12 345."},
        ],
    }


async def test_the_chat_asked_is_the_project_of_the_session_under_the_owner(history):
    h = await history([{"messages": turns()}])

    await h.client.get(f"/chat/history/{SESSION}")

    headers, _params = h.server.history_calls[0]
    assert headers["x-ouroboros-owner-id"] == str(USER)
    assert headers["x-ouroboros-chat-id"] == PROJECT


async def test_a_turn_without_a_project_has_the_session_as_its_chat(history):
    h = await history([{"messages": turns()}])
    h.dialogs[USER] = {"projects": [], "folders": []}

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert got.status_code == 200
    assert h.server.history_calls[0][0]["x-ouroboros-chat-id"] == SESSION


async def test_older_turns_are_read_from_older_pages_and_come_in_order(history):
    older = [
        history_record("user", "первый вопрос", RUN_1, ts="2026-10-17T10:00:01+00:00"),
        history_record("assistant", "первый ответ", RUN_1, task_id=ROOT_1, ts="2026-10-17T10:00:03+00:00"),
    ]
    newer = [
        history_record("user", "второй вопрос", RUN_2, ts="2026-10-18T10:00:01+00:00"),
        history_record("assistant", "второй ответ", RUN_2, task_id=ROOT_2, ts="2026-10-18T10:00:03+00:00"),
    ]
    h = await history([
        {"messages": newer, "has_more": True, "next_before_ts": "2026-10-18T10:00:01+00:00"},
        {"messages": older},
    ])

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert [m["content"] for m in got.json()["history"]] == [
        "первый вопрос", "первый ответ", "второй вопрос", "второй ответ",
    ]
    assert unquote(h.server.history_calls[1][1]["before_ts"]) == "2026-10-18T10:00:01+00:00"


async def test_a_turn_still_running_shows_the_question_without_an_answer(history):
    h = await history([{"messages": turns()[:1]}])

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert got.json()["history"] == [{"role": "user", "content": "какая выручка?"}]


async def test_only_the_last_two_hundred_messages_are_returned(history):
    records = []
    for index in range(150):
        run = f"{index:08d}-0000-4000-8000-000000000000"
        stamp = f"2026-10-18T10:{index // 60:02d}:{index % 60:02d}+00:00"
        records += [
            history_record("user", f"q{index}", run, ts=stamp),
            history_record("assistant", f"a{index}", run, task_id=f"root{index}", ts=stamp),
        ]
    h = await history([{"messages": records}])

    got = (await h.client.get(f"/chat/history/{SESSION}")).json()["history"]

    assert len(got) == 200 and got[0]["content"] == "q50" and got[-1]["content"] == "a149"


async def test_a_session_without_turns_is_not_found(history):
    h = await history([{"messages": []}])

    assert (await h.client.get(f"/chat/history/{SESSION}")).status_code == 404


async def test_another_owner_does_not_get_the_history_of_a_session_that_is_not_theirs(history):
    """C1.3: чужой владелец — 404. Его чат — не проект владельца сессии, а его собственная пустая сессия."""
    h = await history([{"messages": []}])
    h.current["id"] = USER + 1

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert got.status_code == 404
    headers, _params = h.server.history_calls[0]
    assert headers["x-ouroboros-owner-id"] == str(USER + 1) and headers["x-ouroboros-chat-id"] == SESSION


async def test_an_ouroboros_that_is_down_is_a_503_with_a_text(history):
    h = await history([503])

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert got.status_code == 503
    assert "Повторите" in got.json()["detail"]


async def test_a_malformed_session_id_is_a_400(history):
    h = await history([{"messages": []}])

    assert (await h.client.get("/chat/history/not a session")).status_code in (400, 404)
    assert (await h.client.get("/chat/history/zz")).status_code == 400
    assert h.server.history_calls == []
