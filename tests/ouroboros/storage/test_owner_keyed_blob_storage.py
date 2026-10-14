"""Two owners share one relative path and never each other's bytes.

Isolation here is by layout: the physical key carries an owner segment for the scopes in
``OWNER_KEYED_SCOPES``. Layout is not authorization — the record's owner field and the
route comparison are ``scope-attachment-records-and-routes`` — so these tests pin the
layout and say nothing about who may ask.
"""

from __future__ import annotations

import json
import pathlib
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.blob.keys import build_object_key, owner_key_segment
from ouroboros.storage.blob.scopes import ALL_SCOPES, OWNER_KEYED_SCOPES, SCOPE_UPLOADS
from ouroboros.storage.chat_attachments import cleanup_expired_attachments, get_attachment
from ouroboros.storage.file_catalog import FileCatalog
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.tools.core import _data_list, _data_read, _data_write
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.files import stored_file
from tests.helpers.shared import TEST_SCOPE, FakeCancellation, make_tool_ctx, scope_with

_BOB = scope_with(TEST_SCOPE, owner_id="bob")


@pytest.fixture(autouse=True)
def _multi_owner_mode(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")


def _ctx(tmp_path: pathlib.Path, scope):
    return make_tool_ctx(repo_dir=tmp_path / "repo", drive_root=tmp_path, scope=scope, task_id="task")


def test_two_owners_write_one_relative_path(tmp_path):
    alice, bob = _ctx(tmp_path, TEST_SCOPE), _ctx(tmp_path, _BOB)

    assert _data_write(alice, "uploads/report.pdf", "alice content").startswith("OK")
    assert _data_write(bob, "uploads/report.pdf", "bob content").startswith("OK")
    assert _data_write(alice, "uploads/alice-only.txt", "a").startswith("OK")
    assert _data_write(bob, "uploads/bob-only.txt", "b").startswith("OK")

    assert "alice content" in _data_read(alice, "uploads/report.pdf")
    assert "bob content" in _data_read(bob, "uploads/report.pdf")
    listed = json.loads(_data_list(alice, "uploads"))
    assert "alice-only.txt" in listed
    assert "bob-only.txt" not in listed


def test_data_list_does_not_show_another_owners_attachment_dir(tmp_path):
    alice, bob = _ctx(tmp_path, TEST_SCOPE), _ctx(tmp_path, _BOB)
    provider = get_storage_provider()
    ids = {}
    for scope in (TEST_SCOPE, _BOB):
        attachment_id = uuid.uuid4().hex
        ids[scope.owner_id] = attachment_id
        stored_file(
            scope=scope,
            provider=provider,
            data=b"PK\x03\x04payload",
            attachment_id=attachment_id,
            filename="salary.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            source="browser",
        )

    alice.attachments = [{"id": ids["global"]}]
    bob.attachments = [{"id": ids["bob"]}]
    assert json.loads(_data_list(alice, "uploads/chat")) == [f"{ids['global']}/"]
    assert json.loads(_data_list(bob, "uploads/chat")) == [f"{ids['bob']}/"]
    assert get_attachment(TEST_SCOPE, ids["bob"]) is None


@pytest.mark.parametrize("scope", sorted(ALL_SCOPES - OWNER_KEYED_SCOPES))
def test_non_owner_keyed_scopes_keep_their_keys(scope):
    key = "dir/object.json"

    assert build_object_key(TEST_SCOPE, scope, key) == build_object_key(_BOB, scope, key)


def test_the_owner_keyed_scope_does_not_keep_its_key():
    key = "chat/abc/report.pdf"

    assert build_object_key(TEST_SCOPE, SCOPE_UPLOADS, key) != build_object_key(_BOB, SCOPE_UPLOADS, key)
    assert f"owners/{owner_key_segment('global')}/" in build_object_key(TEST_SCOPE, SCOPE_UPLOADS, key)


def test_data_list_never_returns_the_owner_segment_directory(tmp_path):
    alice, bob = _ctx(tmp_path, TEST_SCOPE), _ctx(tmp_path, _BOB)
    assert _data_write(bob, "uploads/bob.txt", "b").startswith("OK")

    listed = json.loads(_data_list(alice, "uploads"))

    assert not any(entry.startswith("owners") for entry in listed)
    assert listed in ([], ["⚠️ Directory not found: uploads"])


def _initialize_store(tmp_path: pathlib.Path):

    return runtime_persistence.initialize()


def _seed_expired_attachment(tmp_path: pathlib.Path, provider, scope, marker: str) -> str:
    from sqlalchemy import text

    from ouroboros.storage.chat_store import append_chat_message

    attachment_id = f"attachment-{marker}" * 2
    metadata = stored_file(
        scope=scope,
        provider=provider,
        data=b"old",
        attachment_id=attachment_id,
        filename="old.txt",
        mime="text/plain",
        source="browser",
    )
    with runtime_persistence.current.engine.begin() as conn:
        conn.execute(
            text("UPDATE files SET status='draft', expires_at='2000-01-01' WHERE id=:id"), {"id": metadata["id"]}
        )
    append_chat_message(
        tmp_path,
        scope=scope,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="here",
        source="browser",
        client_message_id=f"m-{marker}",
    )
    return attachment_id


def test_attachment_cleanup_sweeps_every_owner_the_database_knows(tmp_path):

    store = _initialize_store(tmp_path)
    provider = get_storage_provider()
    ids = [
        _seed_expired_attachment(tmp_path, provider, scope, str(index))
        for index, scope in enumerate((TEST_SCOPE, _BOB))
    ]

    assert {pair[0] for pair in sql_conversation_repository(store.engine).list_owner_agent_pairs()} == {"global", "bob"}
    assert cleanup_expired_attachments(provider, cancellation=FakeCancellation()) == 2
    for scope, attachment_id in zip((TEST_SCOPE, _BOB), ids, strict=True):
        assert get_attachment(scope, attachment_id)["status"] == "deleted"


def test_attachment_cleanup_skips_files_claimed_by_another_replica(tmp_path):
    store = _initialize_store(tmp_path)
    provider = get_storage_provider()
    scopes = [scope_with(TEST_SCOPE, owner_id=f"owner-{letter}") for letter in "abc"]
    ids = [_seed_expired_attachment(tmp_path, provider, scope, scope.owner_id) for scope in scopes]

    with ThreadPoolExecutor(1) as pool:
        with store.engine.begin() as conn:
            claimed = FileCatalog.claim_cleanup(conn, limit=1, lease_seconds=300)[0]
            cleanup = pool.submit(cleanup_expired_attachments, provider, cancellation=FakeCancellation())
            assert cleanup.result(timeout=2) == 2
    for scope, id_ in zip(scopes, ids, strict=True):
        assert get_attachment(scope, id_)["status"] == ("deleting" if id_ == claimed.id else "deleted")


def test_attachment_cleanup_stops_on_a_set_stop_event(tmp_path):

    _initialize_store(tmp_path)
    provider = get_storage_provider()
    scope = scope_with(TEST_SCOPE, owner_id="owner-a")
    attachment_id = _seed_expired_attachment(tmp_path, provider, scope, "stopped")
    cancellation = FakeCancellation()
    cancellation.request_stop()

    assert cleanup_expired_attachments(provider, cancellation=cancellation) == 0
    assert get_attachment(scope, attachment_id) is not None
