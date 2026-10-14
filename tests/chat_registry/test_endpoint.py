"""The third owner-bearing route group: a list, a rename, a retirement, over one owner's rows."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import config_registry.chat_registry.api.http.endpoint as endpoint
from config_registry.chat_registry.domain import ChatEntity, TitleSource
from config_registry.chat_registry.interfaces import IChatRegistryRepo
from infra.utils.http.fastapi import install_trace_middleware
from tests.helpers.agent_registry import FakeAgentRegistry

OWNER_HEADER = "X-Ouroboros-Owner-Id"


class FakeChatRegistry(IChatRegistryRepo):
    """The table, keyed the way every statement is: owner, agent and chat together."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], ChatEntity] = {}

    def add(self, owner_id: str, agent_id: str, chat_id: str, *, title: str = "", created_at: str = "") -> ChatEntity:
        chat = ChatEntity(
            owner_id=owner_id,
            agent_id=agent_id,
            chat_id=chat_id,
            title=title,
            title_source=TitleSource.PROVISIONAL,
            title_attempts=0,
            is_active=True,
            created_at=created_at or f"2026-01-01T00:00:0{len(self.rows)}+00:00",
        )
        self.rows[(owner_id, agent_id, chat_id)] = chat
        return chat

    async def list_chats(self, owner_id: str, agent_id: str, *, include_retired: bool = False) -> list[ChatEntity]:
        found = [
            chat
            for (owner, agent, _chat), chat in self.rows.items()
            if (owner, agent) == (owner_id, agent_id) and (include_retired or chat.is_active)
        ]
        return sorted(found, key=lambda chat: (chat.created_at, chat.chat_id), reverse=True)

    async def get_chat(self, owner_id: str, agent_id: str, chat_id: str) -> ChatEntity | None:
        return self.rows.get((owner_id, agent_id, chat_id))

    async def claim_title_attempt(self, owner_id: str, agent_id: str, chat_id: str, *, max_attempts: int) -> bool:
        current = self.rows[(owner_id, agent_id, chat_id)]
        if current.title_source is not TitleSource.PROVISIONAL or current.title_attempts >= int(max_attempts):
            return False
        self.rows[(owner_id, agent_id, chat_id)] = ChatEntity(
            **{**current.__dict__, "title_attempts": current.title_attempts + 1}
        )
        return True

    async def store_generated_title(self, owner_id: str, agent_id: str, chat_id: str, *, title: str) -> bool:
        current = self.rows[(owner_id, agent_id, chat_id)]
        if current.title_source is not TitleSource.PROVISIONAL:
            return False
        self.rows[(owner_id, agent_id, chat_id)] = ChatEntity(
            **{**current.__dict__, "title": title, "title_source": TitleSource.MODEL}
        )
        return True

    async def rename_chat(self, owner_id: str, agent_id: str, chat_id: str, *, title: str) -> None:
        current = self.rows[(owner_id, agent_id, chat_id)]
        self.rows[(owner_id, agent_id, chat_id)] = ChatEntity(
            **{**current.__dict__, "title": title, "title_source": TitleSource.OWNER}
        )

    async def set_chat_active(self, owner_id: str, agent_id: str, chat_id: str, *, is_active: bool) -> None:
        current = self.rows[(owner_id, agent_id, chat_id)]
        self.rows[(owner_id, agent_id, chat_id)] = ChatEntity(**{**current.__dict__, "is_active": is_active})


@pytest.fixture
def registry() -> FakeAgentRegistry:
    return FakeAgentRegistry()


@pytest.fixture
def chats() -> FakeChatRegistry:
    return FakeChatRegistry()


def _client(registry, chats) -> TestClient:
    app = FastAPI()
    app.include_router(endpoint.router)
    install_trace_middleware(app, source="test")
    app.dependency_overrides[endpoint.get_registry] = lambda: registry
    app.dependency_overrides[endpoint.get_chats] = lambda: chats
    return TestClient(app)


def _with_agent(registry, owner_id: str = "alice", agent_id: str = "main"):
    from config_registry.agent_registry.domain import AgentEntity

    registry.by_owner.setdefault(owner_id, []).append(
        AgentEntity(agent_id=agent_id, owner_id=owner_id, name="main", description="", is_active=True)
    )


def test_the_listing_is_active_only_ordered_and_this_owners(registry, chats) -> None:
    _with_agent(registry)
    chats.add("alice", "main", "c1", title="first")
    chats.add("alice", "main", "c2", title="second")
    chats.add("bob", "main", "c3", title="somebody else's")
    chats.rows[("alice", "main", "c1")] = ChatEntity(
        **{**chats.rows[("alice", "main", "c1")].__dict__, "is_active": False}
    )

    with _client(registry, chats) as client:
        listed = client.get("/main", headers={OWNER_HEADER: "alice"}).json()
        with_retired = client.get("/main?include_retired=true", headers={OWNER_HEADER: "alice"}).json()

    assert [chat["chat_id"] for chat in listed] == ["c2"]
    assert [chat["chat_id"] for chat in with_retired] == ["c2", "c1"]


def test_a_rename_records_the_owner_as_the_origin(registry, chats) -> None:
    _with_agent(registry)
    chats.add("alice", "main", "c1", title="opening line")

    with _client(registry, chats) as client:
        response = client.patch(
            "/main", json={"chat_id": "c1", "title": "  мой разговор  "}, headers={OWNER_HEADER: "alice"}
        )

    assert response.status_code == 200
    assert response.json()["title"] == "мой разговор"
    assert response.json()["title_source"] == "owner"


def test_a_retirement_answers_with_the_row_it_changed(registry, chats) -> None:
    """The caller has just changed a row it is about to render; 204 makes it ask again."""
    _with_agent(registry)
    chats.add("alice", "main", "c1", title="opening line")

    with _client(registry, chats) as client:
        retired = client.patch("/main", json={"chat_id": "c1", "is_active": False}, headers={OWNER_HEADER: "alice"})
        restored = client.patch("/main", json={"chat_id": "c1", "is_active": True}, headers={OWNER_HEADER: "alice"})

    assert (retired.status_code, retired.json()["is_active"]) == (200, False)
    assert (restored.status_code, restored.json()["is_active"]) == (200, True)


def test_another_owners_conversation_is_a_missing_resource(registry, chats) -> None:
    _with_agent(registry)
    chats.add("bob", "main", "c1", title="bob's")

    with _client(registry, chats) as client:
        response = client.patch("/main", json={"chat_id": "c1", "title": "mine now"}, headers={OWNER_HEADER: "alice"})

    assert response.status_code == 404
    assert chats.rows[("bob", "main", "c1")].title == "bob's"


def test_an_agent_the_owner_does_not_have_is_a_missing_resource(registry, chats) -> None:
    _with_agent(registry, owner_id="bob", agent_id="bobs-agent")

    with _client(registry, chats) as client:
        listed = client.get("/bobs-agent", headers={OWNER_HEADER: "alice"})

    assert listed.status_code == 404


def test_an_empty_rename_title_is_refused_as_malformed(registry, chats) -> None:
    _with_agent(registry)
    chats.add("alice", "main", "c1", title="opening line")

    with _client(registry, chats) as client:
        response = client.patch("/main", json={"chat_id": "c1", "title": "   "}, headers={OWNER_HEADER: "alice"})

    assert response.status_code == 400
    assert chats.rows[("alice", "main", "c1")].title == "opening line"


def test_a_rename_is_normalized_and_bounded_like_every_other_writer(registry, chats) -> None:
    """The column carries no length CHECK, so the writer is the only place a bound can exist."""
    from config_registry.chat_registry.domain import TITLE_MAX_LENGTH

    _with_agent(registry)
    chats.add("alice", "main", "c1", title="opening line")

    with _client(registry, chats) as client:
        response = client.patch(
            "/main",
            json={"chat_id": "c1", "title": f"  первая\x00строка  {'я' * 200}\nвторая строка"},
            headers={OWNER_HEADER: "alice"},
        )

    assert response.status_code == 200
    stored = chats.rows[("alice", "main", "c1")].title
    assert len(stored) == TITLE_MAX_LENGTH
    assert stored.startswith("первая строка")
    assert "\x00" not in stored and "вторая" not in stored


def test_a_rename_of_nothing_but_control_characters_is_refused(registry, chats) -> None:
    """Empty is judged after normalization, or the owner is handed back a blank row."""
    _with_agent(registry)
    chats.add("alice", "main", "c1", title="opening line")

    with _client(registry, chats) as client:
        response = client.patch("/main", json={"chat_id": "c1", "title": "\x00\x07\t"}, headers={OWNER_HEADER: "alice"})

    assert response.status_code == 400
    assert chats.rows[("alice", "main", "c1")].title == "opening line"


def test_a_synthetic_chat_id_is_refused_rather_than_reported_absent(registry, chats) -> None:
    """Not-found would say the conversation does not exist; the truth is it may not be asked for."""
    _with_agent(registry)

    with _client(registry, chats) as client:
        for chat_id in ("-1", "-4001", ""):
            response = client.patch("/main", json={"chat_id": chat_id, "title": "x"}, headers={OWNER_HEADER: "alice"})
            assert response.status_code == 400, chat_id


@pytest.mark.usefixtures("multiuser_mode")
def test_a_missing_owner_header_is_refused_in_the_mode(registry, chats) -> None:
    with _client(registry, chats) as client:
        response = client.get("/main")

    assert response.status_code == 403
    assert OWNER_HEADER in response.json()["detail"]


def test_the_configured_owner_serves_the_single_owner_deployment(registry, chats) -> None:
    from ouroboros.contracts.owner_scope import configured_owner_id

    _with_agent(registry, owner_id=configured_owner_id())
    chats.add(configured_owner_id(), "main", "c1", title="local")

    with _client(registry, chats) as client:
        listed = client.get("/main").json()

    assert [chat["title"] for chat in listed] == ["local"]
