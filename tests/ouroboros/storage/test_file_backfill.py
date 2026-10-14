import json

import pytest
from sqlalchemy import text

from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.chat_store import append_chat_message
from ouroboros.storage.file_backfill import backfill
from ouroboros.storage.file_catalog import FileCatalog
from ouroboros.storage.message_field_crypto import encrypt_field
from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with


def legacy_artifact_metadata(tool="skill_exec"):
    return {
        "schema_version": 1,
        "origin": "agent_generated",
        "purpose": "artifact",
        "context_eligible": False,
        "authoritative": False,
        "task_id": "legacy-task",
        "chat_id": TEST_SCOPE.chat_id,
        "created_at": "2025-01-01T00:00:00+00:00",
        "created_by_tool": tool,
    }


@parametrize_from_dict(
    {
        "skill_exec": {"tool": "skill_exec", "key": "task_artifacts/legacy-task/report.txt", "source": "unknown"},
        "data_write": {"tool": "data_write", "key": "report.txt", "source": "browser"},
    }
)
def test_backfill_preserves_actual_legacy_task_artifacts(tool, key, source):
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="legacy-task", source=source, scope=TEST_SCOPE.to_dict()))
    provider = get_storage_provider()
    provider.put_bytes(TEST_SCOPE, "uploads", key, b"hello")
    provider.atomic_put_json(
        TEST_SCOPE,
        "uploads",
        key + ".metadata.json",
        legacy_artifact_metadata(tool),
    )
    issues = []
    preview = backfill(TEST_SCOPE, provider, apply=False, report=issues.append)
    assert (preview.imported, preview.skipped, issues) == (1, 0, [])
    assert backfill(TEST_SCOPE, provider, apply=True, report=issues.append) == preview
    with store.engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        file = catalog.resolve_reference(key, explicit_ids=[])
        assert file is not None
        assert (file.producer_task, file.artifact_path, file.storage_key, file.size) == ("legacy-task", key, key, 5)
        file_id = file.id
    repeated = backfill(TEST_SCOPE, provider, apply=True, report=issues.append)
    assert (repeated.imported, repeated.unchanged, repeated.skipped) == (0, 1, 0)
    with store.engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        assert catalog.resolve_reference(key, explicit_ids=[]).id == file_id
    assert provider.get_bytes(TEST_SCOPE, "uploads", key) == b"hello"


@parametrize_from_dict(
    {
        "ready": {"canonical_status": "ready"},
        "deleting": {"canonical_status": "deleting"},
    }
)
def test_backfill_uses_canonical_metadata_when_a_legacy_copy_disagrees(canonical_status):
    store = runtime_persistence.initialize()
    provider = get_storage_provider()
    legacy = dict(
        TEST_SCOPE.to_dict(),
        id="duplicate",
        filename="report.txt",
        mime="text/plain",
        size=5,
        source="browser",
        status="deleting" if canonical_status == "ready" else "ready",
        created_at="2025-01-01T00:00:00+00:00",
        storage_key="chat/duplicate/report.txt",
    )
    provider.put_bytes(TEST_SCOPE, "uploads", legacy["storage_key"], b"hello")
    provider.atomic_put_json(TEST_SCOPE, "uploads", "_meta/attachments/duplicate.json", legacy)
    provider.atomic_put_json(TEST_SCOPE, "uploads", "_meta/chat/duplicate.json", {**legacy, "status": canonical_status})
    issues = []
    preview = backfill(TEST_SCOPE, provider, apply=False, report=issues.append)
    assert backfill(TEST_SCOPE, provider, apply=True, report=issues.append) == preview
    assert preview.imported == 1 and not issues
    with store.engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM files WHERE id='duplicate'")).scalar_one() == canonical_status


@pytest.mark.parametrize("surface,chat", [("browser", "1"), ("dialogs", "77")])
def test_backfill_is_explicit_idempotent_and_refuses_unproven_ownership(tmp_path, surface, chat):
    store = runtime_persistence.initialize()
    provider = get_storage_provider()
    scope = scope_with(TEST_SCOPE, surface_id=surface, chat_id=chat)
    legacy = {
        **scope.to_dict(),
        "id": "legacy-file",
        "filename": "report.txt",
        "mime": "text/plain",
        "size": 5,
        "source": surface,
        "status": "ready",
        "created_at": "2025-01-01T00:00:00+00:00",
        "storage_key": "chat/legacy-file/report.txt",
    }
    provider.put_bytes(TEST_SCOPE, "uploads", legacy["storage_key"], b"hello")
    metadata_key = "_meta/chat/legacy-file.json" if surface == "browser" else "_meta/attachments/legacy-file.json"
    provider.atomic_put_json(TEST_SCOPE, "uploads", metadata_key, legacy)
    provider.atomic_put_json(TEST_SCOPE, "uploads", "_meta/attachments/ambiguous.json", {"id": "ambiguous"})
    provider.put_bytes(TEST_SCOPE, "uploads", "_meta/corrupt.json", b"{")
    message = append_chat_message(
        tmp_path, scope=scope, actor_type="owner", chat_id=chat, user_id=1, text="old report", source=surface
    )
    missing = {**legacy, "id": "missing-file", "storage_key": "chat/missing-file/report.txt"}
    message.metadata["attachments"] = [legacy, missing]
    with store.engine.begin() as conn:
        conn.execute(
            text("UPDATE messages SET metadata_json=:metadata, payload_json=:payload WHERE message_id=:id"),
            {
                "id": message.message_id,
                "metadata": encrypt_field(json.dumps(message.metadata)),
                "payload": encrypt_field(message.model_dump_json()),
            },
        )
    issues = []
    preview = backfill(TEST_SCOPE, provider, apply=False, report=issues.append)
    assert (preview.imported, preview.linked, preview.skipped) == (2, 2, 2)
    with store.engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        assert catalog.entries(None).items == []
    applied = backfill(TEST_SCOPE, provider, apply=True, report=issues.append)
    assert applied == preview
    repeated = backfill(TEST_SCOPE, provider, apply=True, report=issues.append)
    assert (repeated.imported, repeated.linked, repeated.skipped) == (0, 0, 2)
    with store.engine.begin() as conn, FileCatalog(conn, scope) as catalog:
        file = catalog.file("legacy-file")
        assert (file.surface_id, file.chat_id, file.name) == (surface, chat, "report.txt")
    files = {
        item["id"]: item
        for item in runtime_persistence.current.conversations_repo.get_message(message.message_id).attachments
    }
    assert files["legacy-file"]["status"] == "ready"
    assert files["missing-file"]["status"] == "deleted"
    assert provider.get_bytes(TEST_SCOPE, "uploads", legacy["storage_key"]) == b"hello"


@parametrize_from_dict(
    {
        "missing task": {"task_scope": None, "content": True},
        "wrong owner": {"task_scope": TEST_SCOPE.model_copy(update={"owner_id": "other"}), "content": True},
        "wrong chat": {"task_scope": TEST_SCOPE.model_copy(update={"chat_id": "other"}), "content": True},
        "missing content": {"task_scope": TEST_SCOPE, "content": False},
    }
)
def test_legacy_artifact_requires_proven_task_and_content(task_scope, content):
    store = runtime_persistence.initialize()
    if task_scope is not None:
        store.tasks_repo.create_task(TaskRequest(id="legacy-task", source="browser", scope=task_scope.to_dict()))
    provider = get_storage_provider()
    key = "task_artifacts/legacy-task/report.txt"
    if content:
        provider.put_bytes(TEST_SCOPE, "uploads", key, b"hello")
    provider.atomic_put_json(TEST_SCOPE, "uploads", key + ".metadata.json", legacy_artifact_metadata())
    issues = []
    result = backfill(TEST_SCOPE, provider, apply=True, report=issues.append)
    assert (result.imported, result.skipped, len(issues)) == (0, 1, 1)
    with store.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM files")).scalar_one() == 0
