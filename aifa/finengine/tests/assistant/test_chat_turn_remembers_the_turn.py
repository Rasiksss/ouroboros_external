"""`POST /chat/turns` записывает ход для привязки инструментов (CONTRACTS.md §3.0, ADR-0004), ручка прежняя."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from finengine.assistant.turn_record import recall
from finengine.config import settings
from finengine.store import bus as B
from finengine.store import cache as C

USER = 7
OWN, FOREIGN = "doc-own", "doc-foreign"


class Store:
    """Документы: свой принадлежит пользователю, чужой — другому (`_docs_of_the_turn` выбрасывает чужой)."""

    def get_document(self, doc_id: str) -> Any:
        owners = {OWN: USER, FOREIGN: USER + 1}
        return SimpleNamespace(owner_id=owners[doc_id]) if doc_id in owners else None


class NoTurn:
    """Шлюз хода, которому нечего сказать: тест смотрит на запись хода, а не на сам ход."""

    async def events(self, request: Any, *, on_root: Any = None):
        return
        yield


class Agent:
    def session_doc_ids(self, session_id: str, *, auth_user_id: int) -> list[str]:
        return []

    async def query_stream(self, **_: Any):  # pragma: no cover — прогон нас не интересует
        yield {"type": "done"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from finengine.api import main as m
    from finengine.assistant import composition
    from finengine.auth.db import User
    from finengine.auth.dependencies import get_current_user

    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "redis_url", "")
    # Ход пишет свои ходы сессии в общее состояние (`session_turns`): SQLite — у файлового на Windows нет flock.
    monkeypatch.setattr(settings, "state_database_url", f"sqlite:///{tmp_path / 'state.db'}")
    B.reset_bus()
    C._cached = None
    monkeypatch.setattr(composition, "turn_gateway", lambda: NoTurn())
    monkeypatch.setattr(m, "get_agent", lambda: Agent())
    monkeypatch.setattr(m, "get_store", lambda: Store())
    monkeypatch.setattr(m, "_upsert_dialog_stub", lambda *args, **kwargs: None)
    m.app.dependency_overrides[get_current_user] = lambda: User(id=USER, username="analyst")
    try:
        yield TestClient(m.app)
    finally:
        m.app.dependency_overrides.pop(get_current_user, None)
        B.reset_bus()
        C._cached = None


def test_the_turn_is_remembered_with_its_goal_documents_and_identity(client: TestClient) -> None:
    response = client.post(
        "/chat/turns",
        json={
            "message": "какая выручка за 2024 год?",
            "project_id": "project-1",
            "doc_ids": [OWN, FOREIGN],
            "active_doc_id": OWN,
            "role": "cib",
        },
    )

    assert response.status_code == 200
    record = recall(response.json()["run_id"])
    assert record is not None
    assert (record.owner_id, record.chat_id, record.user_id) == (str(USER), "project-1", USER)
    assert record.message == "какая выручка за 2024 год?"
    assert record.doc_ids == (OWN,), "чужой документ в записи хода не остаётся"
    assert (record.active_doc_id, record.role, record.case_id) == (OWN, "cib", "project-1")
    assert record.root_task_id == ""


def test_the_route_answers_as_before(client: TestClient) -> None:
    body = client.post("/chat/turns", json={"message": "привет"}).json()

    assert set(body) == {"schema_version", "event_schema_version", "run_id", "events_url"}
    assert body["events_url"] == f"/chat/runs/{body['run_id']}/events"


@pytest.mark.parametrize("enabled", [False, True])
def test_the_turn_remembers_the_users_web_search_choice(client: TestClient, enabled: bool) -> None:
    response = client.post("/chat/turns", json={"message": "ставка ЦБ", "web_search": enabled})

    record = recall(response.json()["run_id"])
    assert record is not None
    assert record.web_search is enabled
