"""`POST /chat/turns` отдаёт ход в ouroboros через `turn_gateway`; `pinned_tool` остаётся внутри службы (шаг 10, О3).

Настоящие: приложение, `run_orchestrator`, запись хода, `TurnGateway` и `OuroClient` по сети на
подставной сервер ouroboros. Подставные — агент прежнего чата (он не должен быть вызван) и хранилище
документов. Ответ ручки прежний: `{run_id, events_url}` (CONTRACTS.md §1).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fake_ouroboros import FakeOuroboros, assistant, echo
from turn_harness import gateway_of
from websockets.asyncio.server import ServerConnection

from finengine.assistant.turn_record import recall
from finengine.config import settings
from finengine.contracts.transport import CompletedEvent, LegacyCompatibilityEvent
from finengine.skills import run_orchestrator as ro
from finengine.store import bus as B
from finengine.store import cache as C

USER = 7
OWN = "doc-own"


class Store:
    def get_document(self, doc_id: str) -> Any:
        return SimpleNamespace(owner_id=USER) if doc_id == OWN else None


class OldChat:
    """Агент прежнего чата: на ходе через ouroboros его не зовут, на `pinned_tool` — зовут."""

    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def session_doc_ids(self, session_id: str, *, auth_user_id: int) -> list[str]:
        return []

    async def query_stream(self, **turn: Any) -> AsyncIterator[dict[str, Any]]:
        self.turns.append(turn)
        yield {"type": "session_id", "session_id": "old-session"}
        yield {"type": "text_delta", "text": "прямой запуск"}
        yield {"type": "done"}


async def final_answer(connection: ServerConnection, server: FakeOuroboros) -> None:
    asked = await server.read_frame(connection)
    run_id = asked["client_message_id"]
    await server.send(
        connection,
        echo(run_id, asked["content"]),
        assistant(run_id, "Выручка — 987 654.", task_id="r00t"),
    )
    await server.read_until_closed(connection)


@pytest.fixture
async def chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from finengine.api import main as m
    from finengine.assistant import composition
    from finengine.auth.db import User
    from finengine.auth.dependencies import get_current_user

    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "redis_url", "")
    B.reset_bus()
    C._cached = None
    agent, stubs = OldChat(), []
    monkeypatch.setattr(m, "get_agent", lambda: agent)
    monkeypatch.setattr(m, "get_store", lambda: Store())
    monkeypatch.setattr(m, "_upsert_dialog_stub", lambda *args, **kwargs: stubs.append(args))
    m.app.dependency_overrides[get_current_user] = lambda: User(id=USER, username="analyst")
    async with FakeOuroboros(final_answer) as server:
        monkeypatch.setattr(composition, "turn_gateway", lambda: gateway_of(server))
        transport = httpx.ASGITransport(app=m.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
            yield SimpleNamespace(client=client, agent=agent, server=server, stubs=stubs)
    m.app.dependency_overrides.pop(get_current_user, None)
    B.reset_bus()
    C._cached = None
    ro._runs.clear()


async def journal_of(run_id: str) -> list[Any]:
    return [event async for event in ro.subscribe_versioned(run_id, kind="chat")]


def legacy_types(events: list[Any]) -> list[str]:
    return [e.legacy_event["type"] for e in events if isinstance(e, LegacyCompatibilityEvent)]


async def test_a_chat_turn_goes_to_ouroboros_and_its_answer_is_the_answer_of_the_turn(chat) -> None:
    """Ход → ouroboros: сообщение дословно, ответ — `text_delta`, `text_segment_end`, `completed`."""
    response = await chat.client.post(
        "/chat/turns",
        json={"message": "какая выручка за 2024 год?", "project_id": "project-1", "doc_ids": [OWN]},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"schema_version", "event_schema_version", "run_id", "events_url"}
    assert body["events_url"] == f"/chat/runs/{body['run_id']}/events"
    events = await journal_of(body["run_id"])
    types = legacy_types(events)
    assert types[0] == "session_id", "ход начинается с идентификатора сессии, как раньше"
    assert types[-2:] == ["text_delta", "text_segment_end"]
    assert [e for e in events if isinstance(e, LegacyCompatibilityEvent) and e.legacy_event["type"] == "text_delta"][
        0
    ].legacy_event["text"] == "Выручка — 987 654."
    assert isinstance(events[-1], CompletedEvent) and events[-1].status == "succeeded"
    assert chat.server.received[0]["content"] == "какая выручка за 2024 год?"
    assert chat.server.received[0]["client_message_id"] == body["run_id"]
    assert chat.agent.turns == [], "агент прежнего чата и его ветки OpenClaw не вызваны"


async def test_the_chat_in_ouroboros_is_the_project_of_the_user(chat) -> None:
    """ADR-0011, ADR-0014: чат ouroboros = проект, владелец = пользователь входа."""
    response = await chat.client.post("/chat/turns", json={"message": "привет", "project_id": "project-9"})
    await journal_of(response.json()["run_id"])

    handshake = chat.server.handshakes[0]
    assert (handshake["x-ouroboros-owner-id"], handshake["x-ouroboros-chat-id"]) == (str(USER), "project-9")
    record = recall(response.json()["run_id"])
    assert record is not None and (record.owner_id, record.chat_id) == (str(USER), "project-9")


async def test_a_turn_without_a_project_gets_a_chat_of_its_session_not_an_empty_chat(chat) -> None:
    """P59: пустой `project_id` не даёт пустой чат ouroboros — чатом становится сессия хода."""
    response = await chat.client.post("/chat/turns", json={"message": "привет", "session_id": "sess-1"})
    await journal_of(response.json()["run_id"])

    assert chat.server.handshakes[0]["x-ouroboros-chat-id"] == "sess-1"

    response = await chat.client.post("/chat/turns", json={"message": "привет"})
    events = await journal_of(response.json()["run_id"])
    new_session = next(e.legacy_event["session_id"] for e in events if isinstance(e, LegacyCompatibilityEvent))
    assert new_session and chat.server.handshakes[1]["x-ouroboros-chat-id"] == new_session


async def test_a_pinned_tool_button_stays_inside_the_service_and_never_reaches_ouroboros(chat) -> None:
    """О3 (владелец): детерминированный запуск без модели — внутри службы, в историю ouroboros не попадает."""
    response = await chat.client.post(
        "/chat/turns", json={"message": "запусти", "project_id": "project-1", "pinned_tool": "fe_calc"}
    )

    events = await journal_of(response.json()["run_id"])
    assert [t["pinned_tool"] for t in chat.agent.turns] == ["fe_calc"]
    assert "прямой запуск" in [e.legacy_event.get("text") for e in events if isinstance(e, LegacyCompatibilityEvent)]
    assert chat.server.received == [] and chat.server.accepted == 0


async def test_the_dialog_of_the_turn_is_registered_with_the_session_the_screen_will_get(chat) -> None:
    """Список чатов — на службе: ход регистрирует диалог с тем же `session_id`, что уйдёт на экран."""
    response = await chat.client.post("/chat/turns", json={"message": "привет", "project_id": "project-1"})
    events = await journal_of(response.json()["run_id"])

    session = next(e.legacy_event["session_id"] for e in events if isinstance(e, LegacyCompatibilityEvent))
    assert chat.stubs and chat.stubs[0][3] == session
