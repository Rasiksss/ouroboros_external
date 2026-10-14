"""Tests for DB-backed chat storage."""

from __future__ import annotations

import base64
import json

from ouroboros.storage.chat_store import (
    append_chat_message,
    list_chat_history_messages,
    list_owner_messages,
)
from ouroboros.storage.message_field_crypto import reset_encryption_cache_for_tests
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.shared import TEST_SCOPE, scope_with


def test_append_and_list_owner_messages(tmp_path, scope=TEST_SCOPE):
    runtime_persistence.initialize()
    append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="hello",
        source="dialogs",
        client_message_id="m1",
    )
    append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="agent",
        chat_id=1,
        user_id=1,
        text="hi",
        source="dialogs",
        client_message_id="m2",
    )
    messages = list_owner_messages(tmp_path, scope=TEST_SCOPE)
    assert len(messages) == 2
    assert messages[0].content == "hello"
    assert messages[1].content == "hi"


def test_owner_message_window_narrows_by_actor_and_source_before_the_limit(tmp_path):
    """The predicates the caller asked for bound the window, so busier rows cannot crowd it."""
    runtime_persistence.initialize()
    append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="wanted",
        source="browser",
        client_message_id="wanted",
        ts="2026-01-01T00:00:00+00:00",
    )
    for index in range(6):
        append_chat_message(
            tmp_path,
            scope=TEST_SCOPE,
            actor_type="agent" if index % 2 else "owner",
            chat_id=1,
            user_id=1,
            text=f"noise {index}",
            source="browser" if index % 2 else "dialogs",
            client_message_id=f"noise-{index}",
            ts=f"2026-01-01T00:01:0{index}+00:00",
        )

    window = list_owner_messages(
        tmp_path, limit=3, actor_type="owner", source="browser", newest_first=True, scope=TEST_SCOPE
    )

    assert [message.content for message in window] == ["wanted"]


def test_a_stored_row_takes_its_ownership_from_the_scope_and_nothing_else(tmp_path):
    """The axes come from the scope; ``source`` and ``chat_id`` still say what they always did."""
    runtime_persistence.initialize()
    dialogs_scope = scope_with(TEST_SCOPE, surface_id="dialogs", chat_id="dialogs-chat")
    first = append_chat_message(
        tmp_path,
        scope=dialogs_scope,
        actor_type="owner",
        chat_id="dialogs-chat",
        user_id="owner",
        text="from dialogs",
        session_id="session-1",
        source="dialogs",
    )
    second = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="from browser",
        session_id="session-1",
        source="browser",
    )

    assert first is not None
    assert second is not None
    assert (first.owner_id, first.agent_id) == (dialogs_scope.owner_id, dialogs_scope.agent_id)
    assert (second.owner_id, second.agent_id) == (TEST_SCOPE.owner_id, TEST_SCOPE.agent_id)
    assert not hasattr(first, "conversation_id")
    assert first.channel == "dialogs"
    assert second.channel == "browser"


def test_ingress_source_is_normalized_without_losing_channel(tmp_path):
    runtime_persistence.initialize()

    message = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="legacy bridge input",
        session_id="session-1",
        source="ingress",
    )

    assert message is not None
    assert message.source == "dialogs"
    assert message.channel == "ingress"


def test_attachment_metadata_is_not_rendered_into_conversation_text(tmp_path):
    from ouroboros.storage.chat_store import render_chat_history
    from ouroboros.storage.conversation_format import format_messages_for_block
    from tests.helpers.files import stored_file

    attachment = stored_file()
    message = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="agent",
        chat_id=1,
        user_id=1,
        text="Готово",
        source="browser",
        extra_metadata={"attachments": [attachment]},
    )

    block = format_messages_for_block(list_owner_messages(tmp_path, scope=TEST_SCOPE))

    assert "Готово" in block
    assert "api/chat/files" not in block
    assert "storage_key" not in block
    assert message is not None
    assert f"attachment_id={attachment['id']}" in render_chat_history(tmp_path, scope=TEST_SCOPE)


def test_append_chat_message_reports_duplicate_message_id(tmp_path):
    runtime_persistence.initialize()
    first = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="first",
        source="browser",
        client_message_id="duplicate-1",
    )
    duplicate = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="second",
        source="browser",
        client_message_id="duplicate-1",
    )

    assert first is not None
    assert duplicate is None
    assert [message.content for message in list_owner_messages(tmp_path, scope=TEST_SCOPE)] == ["first"]


def test_client_message_id_is_scoped_to_browser_sender_session(tmp_path):
    runtime_persistence.initialize()

    first = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="first tab",
        source="browser",
        sender_session_id="tab-a",
        client_message_id="c-1",
    )
    second = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="second tab",
        source="browser",
        sender_session_id="tab-b",
        client_message_id="c-1",
    )

    assert first is not None and second is not None
    assert first.message_id != second.message_id
    assert first.source_message_id == second.source_message_id == "c-1"


def test_client_message_id_is_scoped_to_dialogs_peer(tmp_path):
    runtime_persistence.initialize()

    first = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id="peer-a",
        user_id="user-a",
        text="first peer",
        source="dialogs",
        client_message_id="mid-1",
    )
    second = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id="peer-b",
        user_id="user-b",
        text="second peer",
        source="dialogs",
        client_message_id="mid-1",
    )

    assert first is not None and second is not None
    assert first.message_id != second.message_id


def test_dialogs_client_message_id_dedupe_survives_session_change(tmp_path):
    runtime_persistence.initialize()

    first = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id="peer-a",
        user_id="user-a",
        text="first delivery",
        session_id="session-before-restart",
        source="dialogs",
        client_message_id="mid-stable",
    )
    duplicate = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id="peer-a",
        user_id="user-a",
        text="replayed delivery",
        session_id="session-after-restart",
        source="dialogs",
        client_message_id="mid-stable",
    )

    assert first is not None
    assert duplicate is None
    assert [message.content for message in list_owner_messages(tmp_path, scope=TEST_SCOPE)] == ["first delivery"]


def _append_history_message(
    tmp_path,
    *,
    message_id: str,
    text: str,
    ts: str,
    chat_id: int = 1,
    source: str = "dialogs",
    is_progress: bool = False,
) -> None:
    append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="agent" if is_progress else "owner",
        chat_id=chat_id,
        user_id=1,
        text=text,
        source=source,
        client_message_id=message_id,
        ts=ts,
        is_progress=is_progress,
    )


def test_a_negative_chat_id_no_longer_hides_a_message(tmp_path):
    """Visibility is decided by ``is_progress`` alone, not by the chat id's sign."""
    from ouroboros.storage.chat_store import list_recent_owner_messages, render_chat_history

    store = runtime_persistence.initialize()
    stored = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=-4001,
        user_id=-4001,
        text="a2a request",
        source="a2a",
        sender_label="a2a:ctx-1",
        client_message_id="task-1",
    )

    assert stored is not None
    assert sql_conversation_repository(store.engine).get_message(stored.message_id) is not None
    assert "a2a request" in render_chat_history(tmp_path, count=100, scope=TEST_SCOPE)
    assert [message.content for message in list_recent_owner_messages(tmp_path, scope=TEST_SCOPE)] == ["a2a request"]


def test_a_negative_chat_id_progress_message_stays_hidden(tmp_path):
    from ouroboros.storage.chat_store import render_chat_history

    runtime_persistence.initialize()
    append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="agent",
        chat_id=-4001,
        user_id=-4001,
        text="still working",
        source="a2a",
        client_message_id="progress-1",
        is_progress=True,
    )

    assert list_chat_history_messages(tmp_path, limit=100, scope=TEST_SCOPE) == []
    assert "still working" not in render_chat_history(tmp_path, count=100, scope=TEST_SCOPE)


def test_updating_metadata_recomputes_chat_history_visibility(tmp_path):
    store = runtime_persistence.initialize()
    stored = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="agent",
        chat_id=-4001,
        user_id=-4001,
        text="reclassified",
        source="a2a",
        client_message_id="progress-2",
        is_progress=True,
    )

    assert stored is not None
    assert list_chat_history_messages(tmp_path, limit=100, scope=TEST_SCOPE) == []

    metadata = dict(stored.metadata)
    metadata.pop("is_progress")
    assert sql_conversation_repository(store.engine).update_message_metadata(stored.message_id, metadata)

    assert [m.content for m in list_chat_history_messages(tmp_path, limit=100, scope=TEST_SCOPE)] == ["reclassified"]


def test_chat_watermark_counts_a_negative_chat_id_message(tmp_path):
    from ouroboros.storage.chat_store import chat_watermark_signature

    runtime_persistence.initialize()
    append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="human message",
        source="dialogs",
    )
    before = chat_watermark_signature(tmp_path, scope=TEST_SCOPE)["message_count"]

    append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=-4001,
        user_id=-4001,
        text="a2a request",
        source="a2a",
        client_message_id="task-1",
    )

    assert chat_watermark_signature(tmp_path, scope=TEST_SCOPE)["message_count"] == before + 1


def test_chat_history_pages_from_newest_after_visibility_filtering(tmp_path):
    runtime_persistence.initialize()
    _append_history_message(
        tmp_path,
        message_id="visible-1",
        text="first visible",
        ts="2026-07-01T10:00:00+00:00",
    )
    _append_history_message(
        tmp_path,
        message_id="visible-a2a",
        text="visible a2a",
        ts="2026-07-01T10:01:00+00:00",
        chat_id=-4001,
        source="a2a",
    )
    _append_history_message(
        tmp_path,
        message_id="hidden-progress",
        text="hidden progress",
        ts="2026-07-01T10:02:00+00:00",
        is_progress=True,
    )
    _append_history_message(
        tmp_path,
        message_id="visible-2",
        text="second visible",
        ts="2026-07-01T10:03:00+00:00",
        source="browser",
    )

    newest = list_chat_history_messages(tmp_path, limit=1, scope=TEST_SCOPE)
    previous = list_chat_history_messages(tmp_path, limit=1, offset=1, scope=TEST_SCOPE)

    assert [message.content for message in newest] == ["second visible"]
    assert [message.content for message in previous] == ["visible a2a"]


def test_chat_history_date_bounds_use_configured_local_timezone(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_TIMEZONE", "Europe/Moscow")
    runtime_persistence.initialize()
    _append_history_message(
        tmp_path,
        message_id="before-local-day",
        text="before local day",
        ts="2026-07-05T20:59:59+00:00",
    )
    _append_history_message(
        tmp_path,
        message_id="inside-local-day",
        text="inside local day",
        ts="2026-07-05T21:00:00+00:00",
    )

    messages = list_chat_history_messages(
        tmp_path,
        scope=TEST_SCOPE,
        limit=10,
        date_from="2026-07-06",
        date_to="2026-07-07",
    )

    assert [message.content for message in messages] == ["inside local day"]


def test_chat_history_tool_has_bounded_date_contract_without_search():
    from ouroboros.tools.service.memory import get_tools

    tool = next(entry for entry in get_tools() if entry.name == "chat_history")
    properties = tool.schema.parameters["properties"]

    assert set(properties) == {"count", "offset", "date_from", "date_to"}
    assert properties["count"]["maximum"] == 200
    assert "search" not in json.dumps(tool.schema.to_dict())


def _test_key_b64() -> str:
    return base64.b64encode(b"k" * 32).decode("ascii")


def _seed_chat_messages(tmp_path, entries: list[tuple[str, str]], *, chat_id: int = 1, source: str = "dialogs") -> None:
    runtime_persistence.initialize()
    for index, (ts, text) in enumerate(entries):
        append_chat_message(
            tmp_path,
            scope=TEST_SCOPE,
            actor_type="owner",
            chat_id=chat_id,
            user_id=1,
            text=text,
            source=source,
            client_message_id=f"m{index}",
            ts=ts,
        )


def test_chat_history_count_reads_latest_messages(tmp_path):
    from ouroboros.storage.chat_store import render_chat_history

    _seed_chat_messages(
        tmp_path,
        [(f"2026-06-23T15:1{index}:00+00:00", f"message-{index}") for index in range(5)],
    )

    history = render_chat_history(tmp_path, count=2, scope=TEST_SCOPE)

    assert "message-3" in history
    assert "message-4" in history
    assert "message-0" not in history
    assert "message-1" not in history


def test_chat_history_offset_pages_from_newest(tmp_path):
    from ouroboros.storage.chat_store import render_chat_history

    _seed_chat_messages(
        tmp_path,
        [(f"2026-06-23T15:1{index}:00+00:00", f"message-{index}") for index in range(5)],
    )

    history = render_chat_history(tmp_path, count=2, offset=1, scope=TEST_SCOPE)

    assert "message-2" in history
    assert "message-3" in history
    assert "message-4" not in history


def test_chat_history_filters_by_created_at_date_range(tmp_path):
    from ouroboros.storage.chat_store import render_chat_history

    _seed_chat_messages(
        tmp_path,
        [
            ("2026-06-23T15:15:00+00:00", "old discussion"),
            ("2026-07-06T16:52:00+00:00", "today discussion"),
        ],
    )

    history = render_chat_history(
        tmp_path,
        scope=TEST_SCOPE,
        count=10,
        date_from="2026-07-06",
        date_to="2026-07-07",
    )

    assert "today discussion" in history
    assert "old discussion" not in history


def test_chat_history_count_is_capped(tmp_path):
    from ouroboros.storage.chat_store import CHAT_HISTORY_MAX_COUNT, render_chat_history

    _seed_chat_messages(
        tmp_path,
        [
            (f"2026-06-23T15:{index // 60:02d}:{index % 60:02d}+00:00", f"message-{index}")
            for index in range(CHAT_HISTORY_MAX_COUNT + 5)
        ],
    )

    history = render_chat_history(tmp_path, count=CHAT_HISTORY_MAX_COUNT + 100, scope=TEST_SCOPE)

    assert f"Showing {CHAT_HISTORY_MAX_COUNT} messages:" in history
    assert "message-0" not in history
    assert f"message-{CHAT_HISTORY_MAX_COUNT + 4}" in history


def test_chat_history_limit_applies_after_the_progress_visibility_marker(tmp_path, monkeypatch):
    from ouroboros.storage.chat_store import render_chat_history

    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", _test_key_b64())
    reset_encryption_cache_for_tests()

    runtime_persistence.initialize()
    append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="visible owner message",
        source="dialogs",
        client_message_id="visible",
        ts="2026-07-06T10:00:00+00:00",
    )
    append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=-4001,
        user_id=1,
        text="visible a2a",
        source="a2a",
        client_message_id="a2a",
        ts="2026-07-06T10:01:00+00:00",
    )
    for index in range(60):
        append_chat_message(
            tmp_path,
            scope=TEST_SCOPE,
            actor_type="agent",
            chat_id=1,
            user_id=1,
            text=f"hidden progress {index}",
            source="dialogs",
            client_message_id=f"progress-{index}",
            ts=f"2026-07-06T10:02:{index:02d}+00:00",
            is_progress=True,
        )

    history = render_chat_history(tmp_path, count=1, scope=TEST_SCOPE)

    # The limit applies to the visible set: the 60 progress rows never enter the count.
    assert "visible a2a" in history
    assert "hidden progress 59" not in history
    assert "visible owner message" not in history

    reset_encryption_cache_for_tests()


# -- a message's identity carries the ownership it is stored under --------------


def test_two_owners_with_one_client_message_id_get_two_rows(tmp_path):
    """A chat id is unique within an owner, and a client mints its own message ids.

    A proxy that numbers each owner's chats from 1, in front of a client using counter
    ids, produces the identical tuple for every owner it fronts.
    """
    runtime_persistence.initialize()
    alice = scope_with(TEST_SCOPE, owner_id="alice")
    bob = scope_with(TEST_SCOPE, owner_id="bob")

    rows = [
        append_chat_message(
            tmp_path,
            scope=owner,
            actor_type="owner",
            chat_id=1,
            user_id=1,
            text=text,
            source="browser",
            sender_session_id="",
            client_message_id="1",
        )
        for owner, text in ((alice, "alice's message"), (bob, "bob's message"))
    ]

    assert all(row is not None for row in rows), "the second owner's insert was refused"
    assert rows[0].message_id != rows[1].message_id
    assert rows[0].dedup_key != rows[1].dedup_key
    assert [row.owner_id for row in rows] == ["alice", "bob"]


def test_a_lookup_does_not_return_another_owners_row(tmp_path):
    from ouroboros.storage.chat_store import get_chat_message_by_client_id

    runtime_persistence.initialize()
    alice = scope_with(TEST_SCOPE, owner_id="alice")
    bob = scope_with(TEST_SCOPE, owner_id="bob")
    append_chat_message(
        tmp_path,
        scope=alice,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="alice's message",
        source="browser",
        client_message_id="1",
    )

    lookup = {"actor_type": "owner", "source": "browser", "chat_id": 1, "user_id": 1, "client_message_id": "1"}

    assert get_chat_message_by_client_id(tmp_path, scope=alice, **lookup) is not None
    assert get_chat_message_by_client_id(tmp_path, scope=bob, **lookup) is None


def test_one_owner_cannot_name_another_owners_row_by_its_stored_id(tmp_path):
    """The raw-id candidate is folded too, or a client could address a foreign row."""
    from ouroboros.storage.chat_store import get_chat_message_by_client_id

    runtime_persistence.initialize()
    alice = scope_with(TEST_SCOPE, owner_id="alice")
    bob = scope_with(TEST_SCOPE, owner_id="bob")
    stored = append_chat_message(
        tmp_path,
        scope=alice,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="alice's message",
        source="browser",
        client_message_id="1",
    )
    assert stored is not None

    found = get_chat_message_by_client_id(
        tmp_path,
        scope=bob,
        actor_type="owner",
        source="browser",
        chat_id=1,
        user_id=1,
        client_message_id=stored.message_id,
    )

    assert found is None


def test_a_message_without_a_client_id_still_carries_a_unique_identity(tmp_path):
    """The column is non-empty on every row, which is what lets the index be plain."""
    runtime_persistence.initialize()

    rows = [
        append_chat_message(
            tmp_path,
            scope=TEST_SCOPE,
            actor_type="agent",
            chat_id=1,
            user_id=1,
            text=text,
            source="browser",
        )
        for text in ("first", "second")
    ]

    assert all(row is not None and row.dedup_key for row in rows)
    assert rows[0].dedup_key != rows[1].dedup_key


_CHAT_A = scope_with(TEST_SCOPE, chat_id="chat-a")
_CHAT_B = scope_with(TEST_SCOPE, chat_id="chat-b")


def _say(scope, text, *, client_message_id=""):
    from ouroboros.storage.chat_store import append_chat_message as append

    return append(
        None,
        scope=scope,
        actor_type="owner",
        chat_id=scope.chat_id,
        user_id=1,
        text=text,
        source="browser",
        client_message_id=client_message_id,
    )


def test_one_client_id_under_two_chats_lands_two_rows(tmp_path):
    """The inner fold already carries the chat, so the derived identities differ."""
    runtime_persistence.initialize()

    first = _say(_CHAT_A, "in chat a", client_message_id="shared-1")
    second = _say(_CHAT_B, "in chat b", client_message_id="shared-1")

    assert first is not None and second is not None
    assert first.dedup_key != second.dedup_key
    assert [m.content for m in list_owner_messages(tmp_path, scope=_CHAT_A)] == ["in chat a"]
    assert [m.content for m in list_owner_messages(tmp_path, scope=_CHAT_B)] == ["in chat b"]


def test_the_same_client_id_twice_in_one_chat_lands_one_row(tmp_path):
    runtime_persistence.initialize()

    assert _say(_CHAT_A, "once", client_message_id="shared-1") is not None
    assert _say(_CHAT_A, "once", client_message_id="shared-1") is None
    assert len(list_owner_messages(tmp_path, scope=_CHAT_A)) == 1


def test_a_lookup_under_one_chat_never_returns_another_chats_row(tmp_path):
    """Whatever key it is asked for: the derived one, and the bare client id."""
    from ouroboros.storage.chat_store import get_chat_message_by_client_id

    runtime_persistence.initialize()
    stored = _say(_CHAT_B, "in chat b", client_message_id="shared-1")
    assert stored is not None

    for client_message_id in ("shared-1", stored.message_id, stored.dedup_key):
        assert (
            get_chat_message_by_client_id(
                tmp_path,
                scope=_CHAT_A,
                actor_type="owner",
                source="browser",
                chat_id=_CHAT_A.chat_id,
                user_id=1,
                client_message_id=client_message_id,
            )
            is None
        )
