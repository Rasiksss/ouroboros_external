from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from ouroboros.storage.chat_attachments import cleanup_expired_attachments, get_attachment
from ouroboros.storage.chat_store import append_chat_message
from ouroboros.storage.file_catalog import FileCatalog, FileError
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.blob_double import InMemoryStorageProvider
from tests.helpers.files import PausingRetirementStorage, stored_file
from tests.helpers.shared import TEST_SCOPE, FakeCancellation


def test_cleanup_observes_cancellation_between_retirements():
    store = runtime_persistence.initialize()
    files = [stored_file(draft=True), stored_file(draft=True)]
    with store.engine.begin() as conn:
        conn.execute(text("UPDATE files SET expires_at='2000-01-01'"))
    cancellation = FakeCancellation()
    provider = PausingRetirementStorage()
    with ThreadPoolExecutor(max_workers=1) as pool:
        cleanup = pool.submit(cleanup_expired_attachments, provider, cancellation=cancellation)
        try:
            assert provider.started.wait(10)
            cancellation.request_stop()
        finally:
            provider.resume.set()
        assert cleanup.result(timeout=10) == 0
    assert len(provider.retired_keys) == 1
    assert all(get_attachment(TEST_SCOPE, file["id"])["status"] == "deleting" for file in files)


def test_cleanup_retries_failed_retirement_without_holding_the_catalog_lock(tmp_path):
    store = runtime_persistence.initialize()
    expired = stored_file(draft=True)
    live = stored_file(draft=True)
    with store.engine.begin() as conn:
        conn.execute(text("UPDATE files SET expires_at='2000-01-01' WHERE id=:id"), {"id": expired["id"]})
    provider = PausingRetirementStorage()
    with ThreadPoolExecutor(max_workers=2) as pool:
        cleanup = pool.submit(cleanup_expired_attachments, provider, cancellation=FakeCancellation())
        try:
            assert provider.started.wait(10)
            with pytest.raises(FileError):
                append_chat_message(
                    tmp_path,
                    scope=TEST_SCOPE,
                    actor_type="owner",
                    chat_id=1,
                    user_id=1,
                    text="expired",
                    source="browser",
                    extra_metadata={"attachments": [expired]},
                )
            persisted = pool.submit(
                append_chat_message,
                tmp_path,
                scope=TEST_SCOPE,
                actor_type="owner",
                chat_id=1,
                user_id=1,
                text="live",
                source="browser",
                extra_metadata={"attachments": [live]},
            ).result(timeout=10)
            assert persisted.attachments[0]["status"] == "ready"
        finally:
            provider.resume.set()
        assert cleanup.result(timeout=10) == 0
    assert get_attachment(TEST_SCOPE, expired["id"])["status"] == "deleting"
    with store.engine.begin() as conn:
        conn.execute(text("UPDATE files SET claim_until=NULL WHERE id=:id"), {"id": expired["id"]})
    assert cleanup_expired_attachments(InMemoryStorageProvider(), cancellation=FakeCancellation()) == 1
    assert get_attachment(TEST_SCOPE, expired["id"])["status"] == "deleted"
    assert get_attachment(TEST_SCOPE, live["id"])["status"] == "ready"


def test_retry_keeps_identity_but_retires_the_abandoned_object_and_fences_late_completion():
    store = runtime_persistence.initialize()
    with store.engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        first = catalog.reserve("report.txt", "text/plain", origin="browser", file_id="retry-id", fingerprint="abc")
        old_key, old_token = first.storage_key, first.claim_token
    with store.engine.begin() as conn:
        conn.execute(text("UPDATE files SET claim_until='2000-01-01' WHERE id=:id"), {"id": first.id})
    with store.engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        retried = catalog.reserve("report.txt", "text/plain", origin="browser", file_id=first.id, fingerprint="abc")
        assert retried.id == first.id and retried.storage_key != old_key
        with pytest.raises(FileError):
            catalog.complete(first.id, size=3, fingerprint="abc", draft=False, token=old_token)
        catalog.complete(retried.id, size=3, fingerprint="abc", draft=False, token=retried.claim_token)
    provider = InMemoryStorageProvider()
    provider.put_bytes(TEST_SCOPE, "uploads", old_key, b"old")
    assert cleanup_expired_attachments(provider, cancellation=FakeCancellation()) == 1
    assert provider.get_bytes(TEST_SCOPE, "uploads", old_key) == b""
    assert get_attachment(TEST_SCOPE, first.id)["status"] == "ready"
