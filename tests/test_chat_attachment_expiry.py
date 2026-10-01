from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier, Event
from typing import Any, BinaryIO

import pytest

from ouroboros.storage.blob.file_backend import FileStorageProvider
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.chat_attachments import (
    AttachmentMetadata,
    attachment_meta_key,
    bind_attachments,
    cleanup_expired_attachments,
    delete_attachment_metadata,
    delete_draft_attachment,
    get_attachment,
    release_attachments,
    store_attachment_bytes,
)

NOW = datetime(2021, 1, 1, tzinfo=timezone.utc)
EXPIRED_AT = "2020-01-01T00:00:00+00:00"
FUTURE_AT = "2030-01-01T00:00:00+00:00"
CHECKPOINT_KEY = "_maintenance/attachment_cleanup.json"
# Concurrency budgets. STEP_TIMEOUT bounds the assertion itself: wall clock
# around real fsync-backed IO, so it is sized for a loaded CI runner rather than
# for a warm local disk. PAUSE_TIMEOUT only guards against a wedged test hanging
# forever, and must stay well above STEP_TIMEOUT — expiring first would release
# a serialized operation and hide the regression these tests exist to catch.
STEP_TIMEOUT = 10.0
PAUSE_TIMEOUT = 60.0


def _put_json(
    provider: FileStorageProvider,
    key: str,
    **value: object,
) -> None:
    provider.atomic_put_json(SCOPE_UPLOADS, key, value)


def _seed_metadata(
    provider: FileStorageProvider,
    storage_key: str,
    *,
    expires_at: str = EXPIRED_AT,
    **fields: object,
) -> str:
    metadata_key = f"_meta/{storage_key}.json"
    provider.put_bytes(SCOPE_UPLOADS, storage_key, storage_key.encode())
    _put_json(
        provider,
        metadata_key,
        storage_key=storage_key,
        expires_at=expires_at,
        **fields,
    )
    return metadata_key


def _store_attachment(
    provider: FileStorageProvider,
    attachment_id: str,
    *,
    data: bytes = b"data",
    filename: str = "file.txt",
    status: str = "draft",
) -> AttachmentMetadata:
    return store_attachment_bytes(
        provider,
        attachment_id=attachment_id,
        filename=filename,
        mime="text/plain",
        data=data,
        source="test",
        status=status,
    )


def _inject_json_change(
    monkeypatch: pytest.MonkeyPatch,
    provider: FileStorageProvider,
    method_name: str,
    key: str,
    change: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    original = getattr(provider, method_name)
    injected = False

    def wrapper(scope: str, current_key: str, *args: Any) -> bool:
        nonlocal injected
        if current_key == key and not injected:
            injected = True
            value = change(args[0]) if callable(change) else change
            provider.atomic_put_json(scope, current_key, value)
        return original(scope, current_key, *args)

    monkeypatch.setattr(provider, method_name, wrapper)


def test_cleanup_pages_metadata_with_one_persisted_cursor(tmp_path) -> None:
    provider = FileStorageProvider(tmp_path)
    expired_first = "chat/attachment-a/old.txt"
    future = "chat/attachment-b/old.txt"
    expired_last = "chat/attachment-c/old.txt"
    _seed_metadata(provider, expired_first)
    future_metadata = _seed_metadata(provider, future, expires_at=FUTURE_AT)
    _seed_metadata(provider, expired_last)

    assert cleanup_expired_attachments(provider, now=NOW, batch_size=2) == 1
    assert provider.get_json(SCOPE_UPLOADS, CHECKPOINT_KEY) == {"cursor": future_metadata}
    assert provider.get_bytes(SCOPE_UPLOADS, future) is not None

    assert cleanup_expired_attachments(provider, now=NOW, batch_size=2) == 1
    assert provider.get_json(SCOPE_UPLOADS, CHECKPOINT_KEY) == {"cursor": ""}
    assert provider.get_bytes(SCOPE_UPLOADS, future) is not None
    assert provider.list_keys(SCOPE_UPLOADS, "chat/") == [future]


def test_cleanup_migrates_deployed_patch_metadata_and_expiry_entries(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FileStorageProvider(tmp_path)
    payload_key = "generated/as_excel_compare/job/report.xlsx"
    metadata_key = _seed_metadata(
        provider,
        payload_key,
        display_name="report.xlsx",
        public_name="report.xlsx",
    )
    alias_key = "_meta/public/report.xlsx.json"
    _put_json(
        provider,
        alias_key,
        storage_key=payload_key,
        expires_at=EXPIRED_AT,
        display_name="report.xlsx",
    )
    old_index = f"_expiry/2020/01/01/00/00/00/000000-{'a' * 64}.json"
    current_index = f"_expiry/2020/01/01/00/00/01/000000-{'b' * 64}.json"
    for key, payload_field in ((old_index, "payload_key"), (current_index, "storage_key")):
        _put_json(provider, key, **{payload_field: payload_key, "metadata_key": metadata_key})

    list_limits: list[tuple[str, int | None]] = []
    list_keys = provider.list_keys

    def recording_list(scope: str, prefix: str, **kwargs: Any) -> list[str]:
        list_limits.append((prefix, kwargs.get("limit")))
        return list_keys(scope, prefix, **kwargs)

    monkeypatch.setattr(provider, "list_keys", recording_list)
    assert cleanup_expired_attachments(provider, now=NOW, batch_size=8) == 1
    assert list_limits == [("_expiry/", 2), ("_meta/", 6)]
    for key in (payload_key, metadata_key, alias_key, old_index, current_index):
        assert not provider.exists(SCOPE_UPLOADS, key)


def test_cleanup_preserves_invalid_and_concurrently_changed_metadata(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FileStorageProvider(tmp_path)
    protected_key = "chat/protected/old.txt"
    mismatched_metadata = "_meta/chat/attachment-a/old.txt.json"
    provider.put_bytes(SCOPE_UPLOADS, protected_key, b"keep")
    _put_json(
        provider,
        mismatched_metadata,
        storage_key=protected_key,
        expires_at=EXPIRED_AT,
    )
    race_key = "chat/attachment-race/old.txt"
    race_metadata = _seed_metadata(provider, race_key)
    race_future = {
        **provider.get_json(SCOPE_UPLOADS, race_metadata),
        "expires_at": FUTURE_AT,
        "status": "deleting",
        "message_id": "message-1",
    }
    alias_key = "_meta/public/report.txt.json"
    expired_alias = {
        "storage_key": "generated/job/report.txt",
        "expires_at": EXPIRED_AT,
    }
    future_alias = {**expired_alias, "expires_at": FUTURE_AT}
    _put_json(provider, alias_key, **expired_alias)
    invalid_expiry = "_meta/chat/attachment-c/old.txt.json"
    malformed_json = "_meta/chat/attachment-d/old.txt.json"
    _put_json(
        provider,
        invalid_expiry,
        storage_key="chat/attachment-c/old.txt",
        expires_at="invalid",
    )
    provider.put_text(SCOPE_UPLOADS, malformed_json, "{")

    _inject_json_change(monkeypatch, provider, "replace_json_if_matches", race_metadata, race_future)
    _inject_json_change(monkeypatch, provider, "delete_json_if_matches", alias_key, future_alias)

    assert cleanup_expired_attachments(provider, now=NOW, batch_size=8) == 0
    assert provider.get_bytes(SCOPE_UPLOADS, protected_key) == b"keep"
    assert provider.get_json(SCOPE_UPLOADS, mismatched_metadata) is not None
    assert provider.get_bytes(SCOPE_UPLOADS, race_key) is not None
    assert provider.get_json(SCOPE_UPLOADS, race_metadata) == race_future
    assert provider.get_json(SCOPE_UPLOADS, alias_key) == future_alias
    assert provider.exists(SCOPE_UPLOADS, invalid_expiry)
    assert provider.exists(SCOPE_UPLOADS, malformed_json)


def test_cleanup_retries_failed_payload_delete(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FileStorageProvider(tmp_path)
    retry_key = "chat/attachment-retry/old.txt"
    retry_metadata = _seed_metadata(provider, retry_key)
    delete = provider.delete

    def fail_payload_delete(scope: str, key: str) -> None:
        if key == retry_key:
            raise OSError("temporary delete failure")
        delete(scope, key)

    monkeypatch.setattr(provider, "delete", fail_payload_delete)
    assert cleanup_expired_attachments(provider, now=NOW) == 0
    assert provider.get_json(SCOPE_UPLOADS, retry_metadata) is not None

    monkeypatch.setattr(provider, "delete", delete)
    assert cleanup_expired_attachments(provider, now=NOW) == 1
    assert provider.get_json(SCOPE_UPLOADS, retry_metadata) is None


def test_duplicate_attachment_id_preserves_original(tmp_path) -> None:
    provider = FileStorageProvider(tmp_path)
    attachment_id = "attachment-duplicate"
    original = _store_attachment(provider, attachment_id, data=b"first", status="ready")

    with pytest.raises(ValueError, match="attachment id already exists"):
        _store_attachment(
            provider,
            attachment_id,
            data=b"second",
            filename="other.txt",
            status="ready",
        )

    assert get_attachment(provider, attachment_id) == original
    assert provider.get_bytes(SCOPE_UPLOADS, original["storage_key"]) == b"first"
    assert provider.list_keys(SCOPE_UPLOADS, "_expiry/") == []


def test_draft_delete_loses_race_to_bind(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FileStorageProvider(tmp_path)
    draft = _store_attachment(
        provider,
        "attachment-delete-race",
        data=b"draft",
        filename="draft.txt",
    )
    _inject_json_change(
        monkeypatch,
        provider,
        "replace_json_if_matches",
        attachment_meta_key(draft["id"]),
        lambda value: {**value, "status": "ready", "message_id": "message-1"},
    )
    with pytest.raises(ValueError, match="changed concurrently"):
        delete_draft_attachment(provider, draft["id"])
    assert provider.get_bytes(SCOPE_UPLOADS, draft["storage_key"]) == b"draft"


def test_legacy_alias_metadata_supports_bind_release_delete(tmp_path) -> None:
    provider = FileStorageProvider(tmp_path)
    attachment_id = "attachment-legacy-alias"
    storage_key = f"chat/{attachment_id}/old.txt"
    metadata_key = f"_meta/{storage_key}.json"
    provider.put_bytes(SCOPE_UPLOADS, storage_key, b"legacy")
    _put_json(
        provider,
        metadata_key,
        storage_key=storage_key,
        display_name="old.txt",
        content_type="text/plain",
        status="draft",
        expires_at=FUTURE_AT,
    )

    bound = bind_attachments(provider, [attachment_id], message_id="message-1")[0]
    assert bound["id"] == attachment_id
    assert bound["filename"] == "old.txt"
    assert bound["mime"] == "text/plain"
    assert bound["status"] == "ready"

    release_attachments(provider, [attachment_id], message_id="message-1")
    assert delete_draft_attachment(provider, attachment_id)
    assert provider.get_bytes(SCOPE_UPLOADS, storage_key) is None
    assert provider.get_json(SCOPE_UPLOADS, metadata_key) is None


@pytest.mark.parametrize("delete_mode", ["draft", "metadata"])
def test_attachment_delete_io_does_not_block_unrelated_bind(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    delete_mode: str,
) -> None:
    provider = FileStorageProvider(tmp_path)
    deleting = _store_attachment(provider, "attachment-delete-a", data=b"delete")
    _store_attachment(provider, "attachment-bind-b", data=b"bind")
    delete_started, finish_delete = Event(), Event()
    original_delete = provider.delete
    delete_operation = {"draft": delete_draft_attachment, "metadata": delete_attachment_metadata}[delete_mode]
    paused_key = deleting["storage_key"] if delete_mode == "draft" else attachment_meta_key(deleting["id"])

    def paused_delete(scope: str, key: str) -> None:
        if key == paused_key:
            delete_started.set()
            assert finish_delete.wait(PAUSE_TIMEOUT)
        original_delete(scope, key)

    monkeypatch.setattr(provider, "delete", paused_delete)
    with ThreadPoolExecutor(max_workers=2) as pool:
        delete_future = pool.submit(delete_operation, provider, deleting["id"])
        assert delete_started.wait(STEP_TIMEOUT)
        try:
            bound = pool.submit(
                bind_attachments,
                provider,
                ["attachment-bind-b"],
                message_id="message-1",
            ).result(timeout=STEP_TIMEOUT)
        finally:
            finish_delete.set()
        delete_future.result(timeout=STEP_TIMEOUT)

    assert bound[0]["status"] == "ready"


def test_unrelated_attachment_uploads_are_not_serialized(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FileStorageProvider(tmp_path)
    barrier = Barrier(2)
    original_put_fileobj = provider.put_fileobj

    def synchronized_put_fileobj(
        scope: str,
        key: str,
        fileobj: BinaryIO,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        barrier.wait(timeout=STEP_TIMEOUT)
        original_put_fileobj(scope, key, fileobj, content_type=content_type)

    monkeypatch.setattr(provider, "put_fileobj", synchronized_put_fileobj)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_store_attachment, provider, attachment_id, data=data, status="ready")
            for attachment_id, data in (
                ("attachment-parallel-a", b"first"),
                ("attachment-parallel-b", b"second"),
            )
        ]
        for future in futures:
            future.result(timeout=PAUSE_TIMEOUT)

    assert provider.get_bytes(SCOPE_UPLOADS, "chat/attachment-parallel-a/file.txt") == b"first"
    assert provider.get_bytes(SCOPE_UPLOADS, "chat/attachment-parallel-b/file.txt") == b"second"
