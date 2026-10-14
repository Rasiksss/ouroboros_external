from __future__ import annotations

import pytest

from ouroboros.storage.chat_store import append_chat_message
from ouroboros.storage.file_catalog import FileCatalog, FileError
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with


@parametrize_from_dict(
    {
        "different_content": {"fingerprint": "changed", "failure": "conflict"},
        "live_upload": {"fingerprint": "original", "failure": "transfer_in_progress"},
    }
)
def test_upload_refusals_expose_domain_meaning(fingerprint, failure):
    engine = runtime_persistence.initialize().engine
    with engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        file = catalog.reserve("report.txt", "text/plain", origin="browser", fingerprint="original")
    with engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        with pytest.raises(FileError) as refused:
            catalog.reserve("report.txt", "text/plain", origin="browser", file_id=file.id, fingerprint=fingerprint)
    assert refused.value.kind.value == failure


def test_folder_tree_is_scoped_and_refuses_cycles_and_nonempty_deletion():
    runtime_persistence.initialize()
    engine = runtime_persistence.current.engine
    with engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        parent = catalog.create_folder("Reports", None)
        child = catalog.create_folder("September", parent.id)
        with pytest.raises(FileError):
            catalog.edit_folder(parent.id, parent_id=child.id, move=True)
        with pytest.raises(FileError):
            catalog.delete_folder(parent.id)
        assert [entry.name for entry in catalog.entries(parent.id).items] == ["September"]
        with FileCatalog(conn, scope_with(TEST_SCOPE, owner_id="someone-else")) as other:
            assert other.entries(None).items == []
            with pytest.raises(FileError):
                other.folder(child.id)
        catalog.edit_folder(child.id, parent_id=None, move=True)
        catalog.delete_folder(parent.id)
        assert [entry.name for entry in catalog.entries(None).items] == ["September"]


def test_message_binding_is_atomic_and_reuses_permanent_file_identity(tmp_path):
    runtime_persistence.initialize()
    store = runtime_persistence.current
    with store.engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        staged = catalog.reserve("report.txt", "text/plain", origin="browser")
        file = catalog.complete(staged.id, size=5, fingerprint="hello", draft=True, token=staged.claim_token)
        file_id, key = file.id, file.storage_key

    def append(ids):
        return append_chat_message(
            tmp_path,
            scope=TEST_SCOPE,
            actor_type="owner",
            chat_id=1,
            user_id=1,
            text="Use these files",
            source="browser",
            extra_metadata={"attachments": [{"id": id_} for id_ in ids]},
        )

    with pytest.raises(FileError):
        append([file_id, "missing-file"])
    assert runtime_persistence.current.conversations_repo.list_messages(scope=TEST_SCOPE) == []
    with store.engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        assert catalog.file(file_id).status == "draft"
    messages = [append([file_id]), append([file_id])]
    assert all(message.attachments[0]["id"] == file_id for message in messages)
    observed = runtime_persistence.current.conversations_repo.get_message(messages[0].message_id)
    updated = {**observed.metadata, "handoff_accepted": True}
    assert runtime_persistence.current.conversations_repo.update_message_metadata(
        observed.message_id, updated, expected_metadata=observed.metadata
    )
    assert not runtime_persistence.current.conversations_repo.update_message_metadata(
        observed.message_id, {}, expected_metadata=observed.metadata
    )
    assert (
        runtime_persistence.current.conversations_repo.get_message(observed.message_id).attachments[0]["filename"]
        == "report.txt"
    )
    with store.engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        file = catalog.file(file_id)
        assert (file.status, file.expires_at, file.storage_key) == ("ready", None, key)
        with pytest.raises(FileError):
            catalog.delete_file(file_id, draft_only=True)


def test_catalog_downgrade_requires_a_restore_instead_of_erasing_file_identity(blank_schema):
    from tests.helpers.migrations import downgrade_to, schema_snapshot, upgrade_to

    engine = blank_schema()
    upgrade_to(engine, "head")
    before = schema_snapshot(engine, data=True)
    with pytest.raises(RuntimeError, match="restore"):
        downgrade_to(engine, "0021")
    assert schema_snapshot(engine, data=True) == before
