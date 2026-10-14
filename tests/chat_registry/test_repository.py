"""Two faces over one table: the transport's and the memory tier's, and the surface's."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from config_registry.chat_registry.domain import TitleSource
from new_core.identity import OwnerScope
from ouroboros.storage.runtime_engine import resolve_runtime_engine


def _scope(owner_id: str = "alice", agent_id: str = "main", chat_id: str = "c1") -> OwnerScope:
    return OwnerScope(owner_id=owner_id, agent_id=agent_id, surface_id="browser", chat_id=chat_id)


def test_a_first_message_records_the_conversation(store, tmp_path) -> None:
    recorded = store.ensure_chat(_scope(), provisional_title="как дела")

    assert (recorded.owner_id, recorded.agent_id, recorded.chat_id) == ("alice", "main", "c1")
    assert (recorded.title, recorded.title_source, recorded.is_active) == ("как дела", TitleSource.PROVISIONAL, True)
    assert store.get_chat(_scope()) == recorded


def test_a_later_message_disturbs_nothing_on_the_record(store) -> None:
    """`DO NOTHING`, so a second message can neither undo a rename nor revive a retirement."""
    store.ensure_chat(_scope(), provisional_title="first")
    store.store_generated_title(_scope(), title="a generated name")

    again = store.ensure_chat(_scope(), provisional_title="second")

    assert (again.title, again.title_source) == ("a generated name", TitleSource.MODEL)


def test_concurrent_first_messages_leave_one_record_and_every_caller_gets_it(store) -> None:
    """Several instances resolve one new chat at once; a read then a write would race."""
    start = threading.Barrier(2)

    def record(title: str):
        start.wait(timeout=5)
        return store.ensure_chat(_scope(), provisional_title=title)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = (
            future.result(timeout=30) for future in [pool.submit(record, "one"), pool.submit(record, "two")]
        )

    assert first == second
    assert first.title in ("one", "two")
    with resolve_runtime_engine("chat registry").connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM chats")).scalar_one() == 1


def test_two_owners_holding_one_chat_id_see_only_their_own(store) -> None:
    store.ensure_chat(_scope(owner_id="alice"), provisional_title="alice's")
    store.ensure_chat(_scope(owner_id="bob"), provisional_title="bob's")

    assert store.get_chat(_scope(owner_id="alice")).title == "alice's"
    assert store.get_chat(_scope(owner_id="bob")).title == "bob's"


def test_two_agents_of_one_owner_keep_separate_conversations(store) -> None:
    store.ensure_chat(_scope(agent_id="main"), provisional_title="with main")
    store.ensure_chat(_scope(agent_id="second"), provisional_title="with second")

    assert store.get_chat(_scope(agent_id="main")).title == "with main"
    assert store.get_chat(_scope(agent_id="second")).title == "with second"


def test_an_unknown_conversation_is_an_absence(store) -> None:
    assert store.get_chat(_scope(chat_id="never-said-anything")) is None


def test_the_attempt_is_claimed_at_most_three_times(store) -> None:
    """The row count is the permission, taken before the call, so a crash burns an attempt."""
    store.ensure_chat(_scope(), provisional_title="opening")

    claims = [store.claim_title_attempt(_scope(), max_attempts=3) for _ in range(4)]

    assert claims == [True, True, True, False]
    assert store.get_chat(_scope()).title_source is TitleSource.PROVISIONAL


def test_a_generated_title_never_overwrites_a_rename(store, chats) -> None:
    store.ensure_chat(_scope(), provisional_title="opening")
    asyncio.run(chats.rename_chat("alice", "main", "c1", title="мой разговор"))

    assert store.store_generated_title(_scope(), title="a model's name") is False
    assert store.get_chat(_scope()).title == "мой разговор"
    assert store.get_chat(_scope()).title_source is TitleSource.OWNER


def test_a_generated_title_is_never_replaced_by_a_second_one(store) -> None:
    """A list the owner is looking at should not rewrite itself."""
    store.ensure_chat(_scope(), provisional_title="opening")

    assert store.store_generated_title(_scope(), title="first name") is True
    assert store.store_generated_title(_scope(), title="second name") is False
    assert store.get_chat(_scope()).title == "first name"


def test_the_listing_is_active_only_ordered_and_deterministic(store, chats) -> None:
    for index, chat_id in enumerate(("c1", "c2", "c3")):
        store.ensure_chat(_scope(chat_id=chat_id), provisional_title=f"chat {index}")
    asyncio.run(chats.set_chat_active("alice", "main", "c2", is_active=False))

    listed = asyncio.run(chats.list_chats("alice", "main"))
    with_retired = asyncio.run(chats.list_chats("alice", "main", include_retired=True))

    assert {chat.chat_id for chat in listed} == {"c1", "c3"}
    assert {chat.chat_id for chat in with_retired} == {"c1", "c2", "c3"}
    assert listed == sorted(listed, key=lambda chat: (chat.created_at, chat.chat_id), reverse=True)


def test_a_listing_never_reaches_another_owners_conversation(store, chats) -> None:
    store.ensure_chat(_scope(owner_id="alice"), provisional_title="alice's")
    store.ensure_chat(_scope(owner_id="bob"), provisional_title="bob's")

    listed = asyncio.run(chats.list_chats("alice", "main"))

    assert [chat.title for chat in listed] == ["alice's"]


def test_a_retirement_leaves_the_row_and_restoring_is_the_same_operation(store, chats) -> None:
    store.ensure_chat(_scope(), provisional_title="opening")

    asyncio.run(chats.set_chat_active("alice", "main", "c1", is_active=False))
    assert store.get_chat(_scope()).is_active is False
    asyncio.run(chats.set_chat_active("alice", "main", "c1", is_active=True))
    assert store.get_chat(_scope()).is_active is True


def test_the_title_is_protected_at_rest_as_the_message_body_is(store, chats, monkeypatch) -> None:
    """A provisional title is a slice of a message the deployment chose to encrypt."""
    import base64

    from ouroboros.storage.message_field_crypto import reset_encryption_cache_for_tests

    engine = resolve_runtime_engine("chat registry")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", base64.b64encode(b"k" * 32).decode("ascii"))
    reset_encryption_cache_for_tests()
    try:
        store.ensure_chat(_scope(), provisional_title="совершенно секретно")
        with engine.connect() as conn:
            stored = conn.execute(text("SELECT title FROM chats")).scalar_one()
        assert str(stored).startswith("enc:v1:")
        assert store.get_chat(_scope()).title == "совершенно секретно"
        assert [chat.title for chat in asyncio.run(chats.list_chats("alice", "main"))] == ["совершенно секретно"]
    finally:
        reset_encryption_cache_for_tests()


def test_the_pair_wide_erasure_takes_every_chat_of_its_pair(store) -> None:
    """One statement for the pair, and another owner's rows are not reachable from it."""
    store.ensure_chat(_scope(chat_id="c1"), provisional_title="совершенно секретно")
    store.ensure_chat(_scope(chat_id="c2"), provisional_title="и это тоже")
    store.ensure_chat(_scope(owner_id="bob"), provisional_title="bob's")

    store.delete_owner_chats(owner_id="alice", agent_id="main")

    assert store.get_chat(_scope(chat_id="c1")) is None
    assert store.get_chat(_scope(chat_id="c2")) is None
    assert store.get_chat(_scope(owner_id="bob")).title == "bob's"


def test_the_async_face_claims_the_same_three_attempts(store, chats) -> None:
    """The memory tier runs on a loop now, so the claim it takes has to be awaitable."""
    store.ensure_chat(_scope(), provisional_title="opening")

    claims = [asyncio.run(chats.claim_title_attempt("alice", "main", "c1", max_attempts=3)) for _ in range(4)]

    assert claims == [True, True, True, False]
    assert store.get_chat(_scope()).title_source is TitleSource.PROVISIONAL


def test_an_awaited_generated_title_never_overwrites_a_rename(store, chats) -> None:
    store.ensure_chat(_scope(), provisional_title="opening")
    asyncio.run(chats.rename_chat("alice", "main", "c1", title="мой разговор"))

    assert asyncio.run(chats.store_generated_title("alice", "main", "c1", title="a model's name")) is False
    assert store.get_chat(_scope()).title == "мой разговор"


def test_an_awaited_generated_title_is_written_once(store, chats) -> None:
    store.ensure_chat(_scope(), provisional_title="opening")

    assert asyncio.run(chats.store_generated_title("alice", "main", "c1", title="first name")) is True
    assert asyncio.run(chats.store_generated_title("alice", "main", "c1", title="second name")) is False
    assert store.get_chat(_scope()).title == "first name"
    assert store.get_chat(_scope()).title_source is TitleSource.MODEL
