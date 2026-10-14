"""Naming a conversation: a state, not an event, and one that costs a lookup when it is done."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from background_memory.handlers.titles import MAX_TITLE_ATTEMPTS, PersistedMessageHandlers, TitleHandler
from background_memory.interfaces import ChatMessagePersistedEvent
from config_registry.chat_registry.domain import TitleSource
from config_registry.chat_registry.infra.repositories.repo import SqlChatRegistryRepo, SqlChatRegistryStore
from ouroboros.storage.runtime_models import ConversationMessage
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.background_memory_db import memory_database
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.user_settings import stub_user_settings

SCOPE = TEST_SCOPE


class FakeLLM:
    def __init__(self, *, content: str = "Про отпуск") -> None:
        self.content, self.calls = content, []

    async def async_chat(self, **kwargs):
        return self.chat(**kwargs)

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {"role": "assistant", "content": self.content, "finish_reason": "stop"}, {}


class ReasoningLLM(FakeLLM):
    """A model that thinks before it answers, charging the preamble to ``max_tokens``."""

    #: What the dev cluster's light model spent reasoning on a six-message chat, rounded up.
    REASONING_TOKENS = 600

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if int(kwargs.get("max_tokens") or 0) <= self.REASONING_TOKENS:
            return {"role": "assistant", "content": "", "finish_reason": "length"}, {}
        return {"role": "assistant", "content": self.content, "finish_reason": "stop"}, {}


#: The light model pinned so no test reaches a configured provider, and no fallback, so a
#: failing primary is the whole of the attempt.
_OWNER = stub_user_settings(light_model="test-model", fallback_model="")


@pytest.fixture
def db(tmp_path):
    yield from memory_database()


@pytest.fixture
def registry(db) -> SqlChatRegistryStore:
    """The synchronous face, which is what the transport writes and what a case asserts on."""
    del db
    return SqlChatRegistryStore()


@pytest.fixture
def chats(db) -> SqlChatRegistryRepo:
    """The async face, which is what the titling step reaches now."""
    return SqlChatRegistryRepo(db.engine)


def _say(db, index: int, actor: str) -> None:
    sql_conversation_repository(db.store.engine).append_message(
        ConversationMessage(
            message_id=f"m-{index}",
            dedup_key=f"m-{index}",
            owner_id=SCOPE.owner_id,
            agent_id=SCOPE.agent_id,
            chat_id=SCOPE.chat_id,
            actor_type=actor,
            content=f"line {index}",
            created_at=f"2026-08-01T00:00:0{index}+00:00",
        ),
        scope=SCOPE,
    )


def _handler(db, chats, llm) -> TitleHandler:
    return TitleHandler(llm, chats, history=db.history, user_settings=_OWNER)


def _event() -> ChatMessagePersistedEvent:
    return ChatMessagePersistedEvent(scope=SCOPE, message_id="m-1")


def test_a_conversation_with_no_record_costs_one_lookup(tmp_path, db, registry, chats) -> None:
    """Dialogs, A2A and the skill bridge write no record; this is the common case."""
    llm = FakeLLM()
    _say(db, 1, "owner")
    _say(db, 2, "agent")

    db.run(_handler(db, chats, llm).handle(_event()))

    assert llm.calls == []


def test_a_one_sided_conversation_claims_no_attempt(tmp_path, db, registry, chats) -> None:
    """A title from the opening question alone is what the provisional title already is."""
    llm = FakeLLM()
    registry.ensure_chat(SCOPE, provisional_title="line 1")
    _say(db, 1, "owner")

    db.run(_handler(db, chats, llm).handle(_event()))

    assert llm.calls == []
    assert registry.get_chat(SCOPE).title_attempts == 0


def test_an_exchange_earns_one_generated_title(tmp_path, db, registry, chats) -> None:
    llm = FakeLLM()
    registry.ensure_chat(SCOPE, provisional_title="line 1")
    _say(db, 1, "owner")
    _say(db, 2, "agent")

    db.run(_handler(db, chats, llm).handle(_event()))

    recorded = registry.get_chat(SCOPE)
    assert (recorded.title, recorded.title_source) == ("Про отпуск", TitleSource.MODEL)
    assert len(llm.calls) == 1


def test_a_reasoning_models_preamble_fits_inside_the_title_budget(tmp_path, db, registry, chats) -> None:
    """`max_tokens` bounds the whole completion, so a cap sized for six words answers nothing."""
    llm = ReasoningLLM()
    registry.ensure_chat(SCOPE, provisional_title="line 1")
    _say(db, 1, "owner")
    _say(db, 2, "agent")

    db.run(_handler(db, chats, llm).handle(_event()))

    recorded = registry.get_chat(SCOPE)
    assert (recorded.title, recorded.title_source) == ("Про отпуск", TitleSource.MODEL)


def test_the_primary_may_burn_at_most_half_the_naming_budget(tmp_path, db, registry, chats) -> None:
    """One deadline covers both attempts: a primary handed the whole of it starves the fallback."""
    from background_memory.handlers import titles

    llm = FakeLLM()
    registry.ensure_chat(SCOPE, provisional_title="line 1")
    _say(db, 1, "owner")
    _say(db, 2, "agent")

    db.run(_handler(db, chats, llm).handle(_event()))

    (call,) = llm.calls
    assert call["read_timeout_sec"] == titles._TITLE_READ_TIMEOUT_SEC
    # `chat_with_fallback` caps each attempt at what is left of the deadline, so a read window of
    # half the budget is what leaves the second attempt a window of its own.
    assert call["timeout_sec"] >= 2 * call["read_timeout_sec"] - 1.0
    assert call["timeout_sec"] <= titles._TITLE_BUDGET_SEC


def test_a_titled_conversation_is_never_titled_again(tmp_path, db, registry, chats) -> None:
    """A list the owner is looking at should not rewrite itself."""
    llm = FakeLLM()
    registry.ensure_chat(SCOPE, provisional_title="line 1")
    _say(db, 1, "owner")
    _say(db, 2, "agent")
    handler = _handler(db, chats, llm)

    db.run(handler.handle(_event()))
    db.run(handler.handle(_event()))

    assert len(llm.calls) == 1


def test_an_owners_rename_is_never_overwritten(tmp_path, db, registry, chats) -> None:
    llm = FakeLLM()
    registry.ensure_chat(SCOPE, provisional_title="line 1")
    _say(db, 1, "owner")
    _say(db, 2, "agent")

    assert registry.claim_title_attempt(SCOPE, max_attempts=MAX_TITLE_ATTEMPTS)
    # The rename lands while the model is answering: the write below matches no row.
    with runtime_persistence.current.engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(
            text("UPDATE chats SET title = 'мой разговор', title_source = 'owner'"),
        )
    db.run(_handler(db, chats, llm).handle(_event()))

    recorded = registry.get_chat(SCOPE)
    assert (recorded.title, recorded.title_source) == ("мой разговор", TitleSource.OWNER)


def test_a_conversation_spends_at_most_three_attempts(tmp_path, db, registry, chats) -> None:
    """A crash mid-call burns an attempt rather than looping; the cap is the record's."""

    class Failing(FakeLLM):
        def chat(self, **kwargs):
            self.calls.append(kwargs)
            raise RuntimeError("provider outage")

    llm = Failing()
    registry.ensure_chat(SCOPE, provisional_title="line 1")
    _say(db, 1, "owner")
    _say(db, 2, "agent")
    handler = _handler(db, chats, llm)

    for _ in range(5):
        db.run(handler.handle(_event()))

    assert len(llm.calls) == MAX_TITLE_ATTEMPTS
    assert registry.get_chat(SCOPE).title_source is TitleSource.PROVISIONAL


def test_a_leased_ownership_pair_does_not_withhold_a_title(tmp_path, db, registry, chats) -> None:
    """The titling step inherits neither the consolidation lease nor its thresholds."""
    llm = FakeLLM()
    registry.ensure_chat(SCOPE, provisional_title="line 1")
    _say(db, 1, "owner")
    _say(db, 2, "agent")
    assert db.run(
        db.leases.acquire(
            SCOPE,
            holder_id="somebody-else",
            now_iso="2026-01-01T00:00:00+00:00",
            expires_at_iso="2999-01-01T00:00:00+00:00",
        )
    )

    db.run(_handler(db, chats, llm).handle(_event()))

    assert registry.get_chat(SCOPE).title == "Про отпуск"
    assert db.run(db.leases.read(SCOPE)).holder_id == "somebody-else"


def test_the_composite_calls_both_steps_and_returns_consolidations_follow_up() -> None:
    calls: list[str] = []
    follow_up = object()

    async def _title(event):
        calls.append("title")

    async def _consolidation(event):
        calls.append("consolidation")
        return follow_up

    answer = asyncio.get_event_loop().run_until_complete(
        PersistedMessageHandlers(SimpleNamespace(handle=_title), SimpleNamespace(handle=_consolidation)).handle(
            _event()
        )
    )

    assert calls == ["title", "consolidation"]
    assert answer is follow_up
