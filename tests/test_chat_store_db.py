"""Tests for DB-backed chat storage."""

from __future__ import annotations

import base64
import json
from typing import get_type_hints

from ouroboros.storage.chat_store import (
    append_chat_message,
    clear_owner_chat_history,
    list_chat_history_messages,
    list_owner_messages,
)
from ouroboros.storage.message_field_crypto import reset_encryption_cache_for_tests
from ouroboros.storage.runtime_models import TaskRecord
from ouroboros.storage.runtime_store import get_runtime_store, init_runtime_store


def test_attachment_metadata_has_named_contract():
    from ouroboros.storage.chat_attachments import (
        AttachmentMetadata,
        get_attachment,
        register_attachment,
    )

    assert get_type_hints(register_attachment)["return"] is AttachmentMetadata
    assert get_type_hints(get_attachment)["return"] == AttachmentMetadata | None


def test_append_and_list_owner_messages(tmp_path):
    init_runtime_store(tmp_path)
    append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="hello",
        source="dialogs",
        client_message_id="m1",
    )
    append_chat_message(
        tmp_path,
        actor_type="agent",
        chat_id=1,
        user_id=1,
        text="hi",
        source="dialogs",
        client_message_id="m2",
    )
    messages = list_owner_messages(tmp_path)
    assert len(messages) == 2
    assert messages[0].content == "hello"
    assert messages[1].content == "hi"


def test_newest_first_cuts_the_window_from_the_newest_end(tmp_path):
    """A bounded read of a long history must reach the newest messages, and still
    hand them back in the order they were said."""
    init_runtime_store(tmp_path)
    for index in range(5):
        append_chat_message(
            tmp_path,
            actor_type="owner",
            chat_id=1,
            user_id=1,
            text=f"m{index}",
            source="dialogs",
            ts=f"2026-01-01T00:00:{index:02d}+00:00",
            client_message_id=f"c{index}",
        )

    assert [m.content for m in list_owner_messages(tmp_path, limit=2, newest_first=True)] == ["m3", "m4"]
    assert [m.content for m in list_owner_messages(tmp_path, limit=2)] == ["m0", "m1"]


def test_store_keeps_explicit_conversation_and_falls_back_to_session(tmp_path):
    """The store is transport-neutral: the ingress/egress that knows the
    transport supplies ``conversation_id`` (Dialogs passes the chat key);
    without one the message joins the session-scoped conversation."""
    init_runtime_store(tmp_path)
    first = append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id="dialogs-chat",
        user_id="owner",
        text="from dialogs",
        session_id="session-1",
        source="dialogs",
        conversation_id="dialogs:dialogs-chat",
    )
    second = append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="from browser",
        session_id="session-1",
        source="browser",
    )

    assert first is not None
    assert second is not None
    assert first.conversation_id == "dialogs:dialogs-chat"
    assert second.conversation_id == "session-1"
    assert first.channel == "dialogs"
    assert second.channel == "browser"


def test_ingress_source_is_normalized_without_losing_channel(tmp_path):
    init_runtime_store(tmp_path)

    message = append_chat_message(
        tmp_path,
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

    init_runtime_store(tmp_path)
    message = append_chat_message(
        tmp_path,
        actor_type="agent",
        chat_id=1,
        user_id=1,
        text="Готово",
        source="browser",
        extra_metadata={
            "attachments": [
                {
                    "id": "attachment-1",
                    "url": "api/chat/files/attachment-1",
                    "storage_key": "chat/attachment-1/image.png",
                }
            ]
        },
    )

    block = format_messages_for_block(list_owner_messages(tmp_path))

    assert "Готово" in block
    assert "api/chat/files" not in block
    assert "storage_key" not in block
    assert message is not None
    assert "attachment_id=attachment-1" in render_chat_history(tmp_path)


def test_clear_owner_chat_history_removes_db_messages(tmp_path):
    init_runtime_store(tmp_path)
    append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="hello",
        source="dialogs",
    )
    clear_owner_chat_history(tmp_path)
    assert list_owner_messages(tmp_path) == []
    assert list_owner_messages(tmp_path, include_progress=True) == []
    store = get_runtime_store(tmp_path)
    assert store is not None

    memory_dir = tmp_path / "memory"
    assert not (memory_dir / "dialogue_blocks.json").exists()
    assert not (memory_dir / "dialogue_summary.md").exists()
    assert not (memory_dir / "dialogue_meta.json").exists()


def test_clear_owner_chat_history_preserves_command_and_newer_messages(tmp_path):
    init_runtime_store(tmp_path)
    append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="old",
        source="browser",
        ts="2026-01-01T00:00:01+00:00",
    )
    command = append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="/clean",
        source="browser",
        ts="2026-01-01T00:00:01+00:00",
    )
    append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="new",
        source="browser",
        ts="2026-01-01T00:00:02+00:00",
    )

    assert command is not None
    clear_owner_chat_history(tmp_path, preserve_message_id=command.message_id)

    assert [message.content for message in list_owner_messages(tmp_path)] == ["/clean", "new"]


def test_clear_owner_chat_history_removes_terminal_consolidation_state(tmp_path):
    store = init_runtime_store(tmp_path)
    for task_id in ("cancelled-context", "timed-out-context"):
        store.upsert_task(
            TaskRecord(
                task_id=task_id,
                task_type="context_consolidation",
                status="interrupted",
                input={"pending_summary": "private history", "pending_offset": 17},
            )
        )
    store.upsert_task(TaskRecord(task_id="ordinary", task_type="task", status="completed"))
    store.cancel_task("cancelled-context")
    store.fail_task("timed-out-context", result="hard timeout")

    clear_owner_chat_history(tmp_path)

    assert [store.get_task(task_id) for task_id in ("cancelled-context", "timed-out-context")] == [None, None]
    assert store.get_task("ordinary") is not None


def test_clear_owner_chat_history_leaves_attachments_for_ttl_cleanup(tmp_path):
    from ouroboros.storage.blob.factory import create_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.storage.chat_attachments import store_attachment_bytes

    provider = create_storage_provider(data_dir=tmp_path)
    metadata = store_attachment_bytes(
        provider,
        attachment_id="attachment-clear",
        filename="report.txt",
        mime="text/plain",
        data=b"report",
        source="browser",
        status="ready",
        message_id="mid-clear",
    )
    append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="report",
        source="browser",
        extra_metadata={"attachments": [metadata]},
    )

    clear_owner_chat_history(tmp_path)

    assert provider.get_bytes(SCOPE_UPLOADS, metadata["storage_key"]) == b"report"
    assert list_owner_messages(tmp_path) == []


def test_append_chat_message_reports_duplicate_message_id(tmp_path):
    init_runtime_store(tmp_path)
    first = append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="first",
        source="browser",
        client_message_id="duplicate-1",
    )
    duplicate = append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="second",
        source="browser",
        client_message_id="duplicate-1",
    )

    assert first is not None
    assert duplicate is None
    assert [message.content for message in list_owner_messages(tmp_path)] == ["first"]


def test_client_message_id_is_scoped_to_browser_sender_session(tmp_path):
    init_runtime_store(tmp_path)

    first = append_chat_message(
        tmp_path,
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
    init_runtime_store(tmp_path)

    first = append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id="peer-a",
        user_id="user-a",
        text="first peer",
        source="dialogs",
        client_message_id="mid-1",
    )
    second = append_chat_message(
        tmp_path,
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
    init_runtime_store(tmp_path)

    first = append_chat_message(
        tmp_path,
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
    assert [message.content for message in list_owner_messages(tmp_path)] == ["first delivery"]


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

    store = init_runtime_store(tmp_path)
    stored = append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=-4001,
        user_id=-4001,
        text="a2a request",
        source="a2a",
        sender_label="a2a:ctx-1",
        client_message_id="task-1",
    )

    assert stored is not None
    assert store.get_message(stored.message_id) is not None
    assert "a2a request" in render_chat_history(tmp_path, count=100)
    assert [message.content for message in list_recent_owner_messages(tmp_path)] == ["a2a request"]


def test_a_negative_chat_id_progress_message_stays_hidden(tmp_path):
    from ouroboros.storage.chat_store import render_chat_history

    init_runtime_store(tmp_path)
    append_chat_message(
        tmp_path,
        actor_type="agent",
        chat_id=-4001,
        user_id=-4001,
        text="still working",
        source="a2a",
        client_message_id="progress-1",
        is_progress=True,
    )

    assert list_chat_history_messages(tmp_path, limit=100) == []
    assert "still working" not in render_chat_history(tmp_path, count=100)


def test_updating_metadata_recomputes_chat_history_visibility(tmp_path):
    store = init_runtime_store(tmp_path)
    stored = append_chat_message(
        tmp_path,
        actor_type="agent",
        chat_id=-4001,
        user_id=-4001,
        text="reclassified",
        source="a2a",
        client_message_id="progress-2",
        is_progress=True,
    )

    assert stored is not None
    assert list_chat_history_messages(tmp_path, limit=100) == []

    metadata = dict(stored.metadata)
    metadata.pop("is_progress")
    assert store.update_message_metadata(stored.message_id, metadata)

    assert [m.content for m in list_chat_history_messages(tmp_path, limit=100)] == ["reclassified"]


def test_clear_owner_chat_history_removes_negative_chat_id_messages(tmp_path):
    """``/clean`` reaches synthetic conversations — nothing is exempt by chat id."""
    init_runtime_store(tmp_path)
    append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="human message",
        source="dialogs",
    )
    append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=-4001,
        user_id=-4001,
        text="a2a request",
        source="a2a",
        client_message_id="task-1",
    )

    clear_owner_chat_history(tmp_path)

    assert list_owner_messages(tmp_path, include_progress=True) == []


def test_clear_owner_chat_history_removes_rows_hidden_by_an_earlier_predicate(tmp_path):
    """Hidden is not exempt. Rows an earlier predicate marked invisible — legacy synthetic-id
    traffic — are never re-decided, so ``/clean`` is the only thing that ever removes them; a
    delete path that read the flag would leave them behind for good."""
    from sqlalchemy import text

    init_runtime_store(tmp_path)
    legacy = append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=-4001,
        user_id=-4001,
        text="a2a request from before the rule changed",
        source="a2a",
        client_message_id="task-legacy",
    )
    store = get_runtime_store(tmp_path)
    with store._connection() as conn:
        conn.execute(
            text("UPDATE messages SET chat_history_visible = 0 WHERE message_id = :message_id"),
            {"message_id": legacy.message_id},
        )
        conn.commit()
    assert list_chat_history_messages(tmp_path, limit=10) == []

    clear_owner_chat_history(tmp_path)

    assert store.get_message(legacy.message_id) is None


def test_clear_owner_chat_history_preserves_a_negative_chat_id_message(tmp_path):
    init_runtime_store(tmp_path)
    append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="human message",
        source="dialogs",
        ts="2026-07-01T10:00:00+00:00",
    )
    preserved = append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=-4001,
        user_id=-4001,
        text="a2a request",
        source="a2a",
        client_message_id="task-1",
        ts="2026-07-01T10:01:00+00:00",
    )

    assert preserved is not None
    clear_owner_chat_history(tmp_path, preserve_message_id=preserved.message_id)

    assert [message.content for message in list_owner_messages(tmp_path)] == ["a2a request"]


def test_chat_watermark_counts_a_negative_chat_id_message(tmp_path):
    from ouroboros.storage.chat_store import chat_watermark_signature

    init_runtime_store(tmp_path)
    append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="human message",
        source="dialogs",
    )
    before = chat_watermark_signature(tmp_path)["message_count"]

    append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=-4001,
        user_id=-4001,
        text="a2a request",
        source="a2a",
        client_message_id="task-1",
    )

    assert chat_watermark_signature(tmp_path)["message_count"] == before + 1


def test_chat_history_pages_from_newest_after_visibility_filtering(tmp_path):
    init_runtime_store(tmp_path)
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

    newest = list_chat_history_messages(tmp_path, limit=1)
    previous = list_chat_history_messages(tmp_path, limit=1, offset=1)

    assert [message.content for message in newest] == ["second visible"]
    assert [message.content for message in previous] == ["visible a2a"]


def test_chat_history_date_bounds_use_configured_local_timezone(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_TIMEZONE", "Europe/Moscow")
    init_runtime_store(tmp_path)
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
    init_runtime_store(tmp_path)
    for index, (ts, text) in enumerate(entries):
        append_chat_message(
            tmp_path,
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

    history = render_chat_history(tmp_path, count=2)

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

    history = render_chat_history(tmp_path, count=2, offset=1)

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

    history = render_chat_history(tmp_path, count=CHAT_HISTORY_MAX_COUNT + 100)

    assert f"Showing {CHAT_HISTORY_MAX_COUNT} messages:" in history
    assert "message-0" not in history
    assert f"message-{CHAT_HISTORY_MAX_COUNT + 4}" in history


def test_chat_history_limit_applies_after_the_progress_visibility_marker(tmp_path, monkeypatch):
    from ouroboros.storage.chat_store import render_chat_history

    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", _test_key_b64())
    reset_encryption_cache_for_tests()

    store = init_runtime_store(tmp_path)
    store._dialect = "postgresql"
    append_chat_message(
        tmp_path,
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
            actor_type="agent",
            chat_id=1,
            user_id=1,
            text=f"hidden progress {index}",
            source="dialogs",
            client_message_id=f"progress-{index}",
            ts=f"2026-07-06T10:02:{index:02d}+00:00",
            is_progress=True,
        )

    history = render_chat_history(tmp_path, count=1)

    # The limit applies to the visible set: the 60 progress rows never enter the count.
    assert "visible a2a" in history
    assert "hidden progress 59" not in history
    assert "visible owner message" not in history

    reset_encryption_cache_for_tests()
