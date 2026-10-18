"""`GET /chat/history/{session_id}` отдаёт историю сессии из ouroboros (CONTRACTS.md §1, §2.6, ADR-0011).

Закрывает C1.3: формат прежний `{role, content}`; сообщения подзадач, уведомления и прогресс отфильтрованы;
чужой владелец — 404. Ходы сессии (чат и корень каждого) — настоящая запись службы (`session_turns` поверх
`store/state` на SQLite); ouroboros — подставной сервер истории по сети (`fake_ouroboros.py`), ручка и клиент —
настоящие.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from fake_ouroboros import FakeOuroboros, history_record
from websockets.asyncio.server import ServerConnection

from finengine.assistant import session_turns
from finengine.assistant.ouro_client import OuroClient

USER = 7
SESSION = "5f3c2a10-7d3e-4c1b-9a55-0e2f6d1b8a01"
OTHER_SESSION = "6a4d3b21-8e4f-4d2c-8b66-1f3a7e2c9b12"
PROJECT = "proj-1"
FOLDER = "folder-1"
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


def remember_turn(session_id: str, run_id: str, chat_id: str, root: str = "", owner: int = USER) -> None:
    """Ход сессии так, как его записывает служба: при старте — чат, в конце — корень."""
    session_turns.record_turn(str(owner), session_id, run_id=run_id, chat_id=chat_id)
    if root:
        session_turns.RootRecorder(str(owner), session_id)(run_id, root)


class Harness:
    def __init__(self, server: FakeOuroboros, client: httpx.AsyncClient, current: dict[str, int]):
        self.server, self.client, self.current = server, client, current


@pytest.fixture
async def history(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> AsyncIterator[Any]:
    from finengine.api import main as m
    from finengine.assistant import composition
    from finengine.auth.db import User
    from finengine.auth.dependencies import get_current_user
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "state_database_url", f"sqlite:///{tmp_path / 'state.db'}")
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
        harness = Harness(server, client, current)
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
    remember_turn(SESSION, RUN_1, PROJECT, ROOT_1)
    remember_turn(SESSION, RUN_2, PROJECT, ROOT_2)

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert got.status_code == 200
    assert got.json() == {
        "session_id": SESSION,
        "history": [
            {"role": "user", "content": "какая выручка?"},
            {"role": "assistant", "content": "Выручка 987 654."},
            {"role": "user", "content": "а прибыль?"},
            {"role": "assistant", "content": "Прибыль 12 345."},
        ],
    }


async def test_the_chat_asked_is_the_chat_the_turn_went_to_under_the_owner(history):
    h = await history([{"messages": turns()}])
    remember_turn(SESSION, RUN_1, PROJECT, ROOT_1)

    await h.client.get(f"/chat/history/{SESSION}")

    headers, _params = h.server.history_calls[0]
    assert headers["x-ouroboros-owner-id"] == str(USER)
    assert headers["x-ouroboros-chat-id"] == PROJECT


async def test_a_turn_without_a_project_has_the_session_as_its_chat(history):
    h = await history([{"messages": turns()}])
    remember_turn(SESSION, RUN_1, SESSION, ROOT_1)

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert got.status_code == 200
    assert h.server.history_calls[0][0]["x-ouroboros-chat-id"] == SESSION


async def test_a_chat_in_a_folder_shows_only_its_own_turns(history):
    """Б1 ревью шага 11: чаты одной папки пишут в чат папки (`folder_id ?? id`); история сессии — только её ходы."""
    h = await history([{"messages": turns()}])
    remember_turn(SESSION, RUN_1, FOLDER, ROOT_1)
    remember_turn(OTHER_SESSION, RUN_2, FOLDER, ROOT_2)

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert h.server.history_calls[0][0]["x-ouroboros-chat-id"] == FOLDER
    assert got.json()["history"] == [
        {"role": "user", "content": "какая выручка?"},
        {"role": "assistant", "content": "Выручка 987 654."},
    ]


async def test_a_session_moved_into_a_folder_keeps_its_turns_from_both_chats_in_order(history):
    before = [
        history_record("user", "первый вопрос", RUN_1, ts="2026-10-17T10:00:01+00:00"),
        history_record("assistant", "первый ответ", RUN_1, task_id=ROOT_1, ts="2026-10-17T10:00:03+00:00"),
    ]
    after = [
        history_record("user", "второй вопрос", RUN_2, ts="2026-10-18T10:00:01+00:00"),
        history_record("assistant", "второй ответ", RUN_2, task_id=ROOT_2, ts="2026-10-18T10:00:03+00:00"),
    ]
    h = await history([{"messages": before}, {"messages": after}])
    remember_turn(SESSION, RUN_1, SESSION, ROOT_1)
    remember_turn(SESSION, RUN_2, FOLDER, ROOT_2)

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert [call[0]["x-ouroboros-chat-id"] for call in h.server.history_calls] == [SESSION, FOLDER]
    assert [m["content"] for m in got.json()["history"]] == [
        "первый вопрос", "первый ответ", "второй вопрос", "второй ответ",
    ]


async def test_a_subtask_that_answers_before_its_root_is_not_the_answer_of_the_turn(history):
    """§2.6: финал хода — запись его корня. Подзадача, начатая во время корня (ADR-0046, вариант B), может
    ответить раньше него — её итог в историю хода не входит."""
    records = [
        history_record("user", "какая выручка?", RUN_1, ts="2026-10-18T10:00:01+00:00"),
        history_record("assistant", "итог подзадачи", RUN_1, task_id=SUB, ts="2026-10-18T10:00:02+00:00"),
        history_record("assistant", "Выручка 987 654.", RUN_1, task_id=ROOT_1, ts="2026-10-18T10:00:03+00:00"),
    ]
    h = await history([{"messages": records}])
    remember_turn(SESSION, RUN_1, PROJECT, ROOT_1)

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert got.json()["history"][-1] == {"role": "assistant", "content": "Выручка 987 654."}


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
    remember_turn(SESSION, RUN_1, PROJECT, ROOT_1)
    remember_turn(SESSION, RUN_2, PROJECT, ROOT_2)

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert [m["content"] for m in got.json()["history"]] == [
        "первый вопрос", "первый ответ", "второй вопрос", "второй ответ",
    ]
    assert unquote(h.server.history_calls[1][1]["before_ts"]) == "2026-10-18T10:00:01+00:00"


async def test_reading_stops_once_every_turn_of_the_session_is_found(history):
    newer = [history_record("user", "второй вопрос", RUN_2, ts="2026-10-18T10:00:01+00:00")]
    h = await history([
        {"messages": newer, "has_more": True, "next_before_ts": "2026-10-18T10:00:01+00:00"},
        {"messages": []},
    ])
    remember_turn(SESSION, RUN_2, PROJECT)

    await h.client.get(f"/chat/history/{SESSION}")

    assert len(h.server.history_calls) == 1


async def test_a_turn_still_running_shows_the_question_without_an_answer(history):
    h = await history([{"messages": turns()[:1]}])
    remember_turn(SESSION, RUN_1, PROJECT)

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert got.json()["history"] == [{"role": "user", "content": "какая выручка?"}]


async def test_a_turn_whose_root_was_never_named_shows_its_first_answer(history):
    """Ход оборвался до записи корня: финал — первая непрогрессная запись ассистента с `task_id` (`find_final`)."""
    h = await history([{"messages": turns()[:4]}])
    remember_turn(SESSION, RUN_1, PROJECT)

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert got.json()["history"][-1] == {"role": "assistant", "content": "Выручка 987 654."}


async def test_only_the_last_two_hundred_messages_are_returned(history):
    records = []
    for index in range(150):
        run = f"{index:08d}-0000-4000-8000-000000000000"
        stamp = f"2026-10-18T10:{index // 60:02d}:{index % 60:02d}+00:00"
        records += [
            history_record("user", f"q{index}", run, ts=stamp),
            history_record("assistant", f"a{index}", run, task_id=f"root{index}", ts=stamp),
        ]
        remember_turn(SESSION, run, PROJECT, f"root{index}")
    h = await history([{"messages": records}])

    got = (await h.client.get(f"/chat/history/{SESSION}")).json()["history"]

    assert len(got) == 200 and got[0]["content"] == "q50" and got[-1]["content"] == "a149"


async def test_a_session_without_turns_is_not_found(history):
    h = await history([{"messages": turns()}])

    assert (await h.client.get(f"/chat/history/{SESSION}")).status_code == 404
    assert h.server.history_calls == []


async def test_another_owner_does_not_get_the_history_of_a_session_that_is_not_theirs(history):
    """C1.3: чужой владелец — 404. Ходы сессии ищутся у него, их у него нет — ouroboros не спрашивается."""
    h = await history([{"messages": turns()}])
    remember_turn(SESSION, RUN_1, PROJECT, ROOT_1)
    h.current["id"] = USER + 1

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert got.status_code == 404
    assert h.server.history_calls == []


async def test_an_ouroboros_that_is_down_is_a_503_with_a_text(history):
    h = await history([503])
    remember_turn(SESSION, RUN_1, PROJECT, ROOT_1)

    got = await h.client.get(f"/chat/history/{SESSION}")

    assert got.status_code == 503
    assert "Повторите" in got.json()["detail"]


async def test_a_malformed_session_id_is_a_400(history):
    h = await history([{"messages": []}])

    assert (await h.client.get("/chat/history/not a session")).status_code in (400, 404)
    assert (await h.client.get("/chat/history/zz")).status_code == 400
    assert h.server.history_calls == []
