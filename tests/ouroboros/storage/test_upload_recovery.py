import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event

import anyio
import pytest
from sqlalchemy import event, select

from infra.async_engine import async_engine_factory
from ouroboros.storage.chat_attachments import store_attachment_async, store_attachment_fileobj
from ouroboros.storage.file_catalog import FileCatalog, FileError, FileTransferPending
from ouroboros.storage.file_models import CatalogFile
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.blob_double import InMemoryStorageProvider
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE


@pytest.mark.parametrize("after_write", [False, True])
def test_failed_put_retries_immediately_on_a_new_key(after_write):
    retry_started, finish = Event(), Event()

    class FailingStorage(InMemoryStorageProvider):
        failed = False
        keys = []

        def put_fileobj(self, owner, scope, key, fileobj, **kwargs):
            self.keys.append(key)
            if self.failed:
                retry_started.set()
                assert finish.wait(5)
            if self.failed or after_write:
                super().put_fileobj(owner, scope, key, fileobj, **kwargs)
            if not self.failed:
                self.failed = True
                raise OSError("lost upload")

    provider = FailingStorage()

    def store():
        return store_attachment_fileobj(
            TEST_SCOPE,
            provider,
            attachment_id="retry-file",
            filename="report.txt",
            mime="text/plain",
            fileobj=BytesIO(b"hello"),
            size=5,
            source="browser",
            status="ready",
        )

    with pytest.raises(OSError):
        store()
    with ThreadPoolExecutor(1) as workers:
        pending = workers.submit(store)
        try:
            assert retry_started.wait(5)
            with pytest.raises(FileTransferPending):
                store()
        finally:
            finish.set()
        file = pending.result(timeout=5)
    assert file["status"] == "ready"
    assert len(set(provider.keys)) == 2
    assert provider.get_bytes(TEST_SCOPE, "uploads", file["storage_key"]) == b"hello"


def test_failed_completion_commit_releases_the_original_staging_token():
    engine = runtime_persistence.current.engine
    provider = InMemoryStorageProvider()
    failed = []

    def reject_completion(conn):
        if not failed and conn.scalar(select(CatalogFile.status).where(CatalogFile.id == "commit-retry")) == "ready":
            failed.append(True)
            raise OSError("completion commit failed")

    def store():
        return store_attachment_fileobj(
            TEST_SCOPE,
            provider,
            attachment_id="commit-retry",
            filename="report.txt",
            mime="text/plain",
            fileobj=BytesIO(b"hello"),
            size=5,
            source="browser",
            status="ready",
        )

    event.listen(engine, "commit", reject_completion)
    try:
        with pytest.raises(OSError, match="completion commit"):
            store()
        assert store()["status"] == "ready"
    finally:
        event.remove(engine, "commit", reject_completion)


@parametrize_from_dict(
    {
        "error": {"mode": "error"},
        "task_cancel": {"mode": "task"},
        "anyio_cancel": {"mode": "anyio"},
    }
)
def test_async_failed_put_releases_only_its_staging_claim(tmp_path, mode):
    engine = runtime_persistence.current.engine
    path = tmp_path / "report.txt"
    path.write_text("hello")

    original = {}

    async def scenario():
        async_engine_factory.initialize(runtime_persistence.current.db_urls)
        started, scopes = asyncio.Event(), []

        class Blob:
            async def put(self, *args, **kwargs):
                with engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
                    file = catalog.session.scalar(select(CatalogFile))
                    original.update(key=file.storage_key, token=file.claim_token)
                started.set()
                if mode == "error":
                    raise OSError("PUT failed")
                await asyncio.Event().wait()

        async def upload():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await store_attachment_async(
                    TEST_SCOPE,
                    Blob(),
                    path,
                    filename=path.name,
                    mime="text/plain",
                    size=5,
                    fingerprint="fingerprint",
                    folder_id=None,
                    draft=False,
                )

        task = asyncio.create_task(upload())
        await started.wait()
        if mode == "error":
            with pytest.raises(OSError):
                await task
        elif mode == "task":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            scopes[0].cancel()
            await task
        await async_engine_factory.dispose()

    anyio.run(scenario)
    with engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        first = catalog.session.scalar(select(CatalogFile))
        old_key, old_token = original["key"], original["token"]
        retry = catalog.reserve(
            first.name, first.mime, origin="browser", file_id=first.id, fingerprint=first.fingerprint
        )
        assert retry.storage_key != old_key
        catalog.release_claim(first.id, old_token)
        with pytest.raises(FileError):
            catalog.complete(first.id, size=5, fingerprint="fingerprint", draft=False, token=old_token)
        completed = catalog.complete(retry.id, size=5, fingerprint="fingerprint", draft=False, token=retry.claim_token)
        catalog.release_claim(first.id, old_token)
        assert catalog.file(completed.id).status == "ready"
