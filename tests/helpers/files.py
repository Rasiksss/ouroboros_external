from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4

import anyio
from starlette.applications import Starlette
from starlette.testclient import TestClient

from infra.async_engine import async_engine_factory
from new_core.identity import OwnerScope
from ouroboros.contracts.attachments import AttachmentMetadata, StoredFile
from ouroboros.observability import aef_tracing
from ouroboros.storage.blob.factory import (
    get_storage_provider,
    install_storage_provider_builder_for_tests,
    reset_storage_provider_for_tests,
)
from ouroboros.storage.blob.protocol import StorageProvider
from ouroboros.storage.chat_attachments import file_transaction
from ouroboros.storage.file_transfers import initialize_transfer_limits
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.store import load_settings
from tests.helpers.blob_double import InMemoryStorageProvider
from tests.helpers.shared import TEST_SCOPE
from transport.browser.infra.repositories.owner_history.repo import SqlOwnerHistoryRepository
from transport.browser.router import collect_routes


def stored_file(
    *,
    scope: OwnerScope = TEST_SCOPE,
    draft: bool = False,
    data: bytes = b"hello",
    filename: str = "report.txt",
    mime: str = "text/plain",
    producer_task: str = "",
    artifact_path: str = "",
    attachment_id: str = "",
    provider: StorageProvider | None = None,
    source: str = "",
) -> AttachmentMetadata:
    from ouroboros.storage.chat_attachments import store_attachment_fileobj

    with BytesIO(data) as fileobj:
        return store_attachment_fileobj(
            scope,
            provider if provider is not None else get_storage_provider(),
            attachment_id=attachment_id or uuid4().hex,
            filename=filename,
            mime=mime,
            fileobj=fileobj,
            size=fileobj.getbuffer().nbytes,
            source=source or str(scope.surface_id),
            status="draft" if draft else "ready",
            producer_task=producer_task,
            artifact_path=artifact_path,
        )


def link_file(file, *, scope=TEST_SCOPE):
    from ouroboros.storage.chat_store import append_chat_message
    from ouroboros.storage.settings.paths import load_path_settings

    append_chat_message(
        load_path_settings().data_dir,
        scope=scope,
        actor_type="owner",
        source=str(scope.surface_id),
        chat_id=scope.chat_id,
        user_id=scope.owner_id,
        text="attachment",
        extra_metadata={"attachments": [file]},
    )
    return file


def outbound_bytes(media: StoredFile, scope=TEST_SCOPE):
    from ouroboros.storage.chat_attachments import get_attachment, read_attachment_bytes

    file = get_attachment(scope, media.file_id)
    return read_attachment_bytes(scope, get_storage_provider(), file["storage_key"], mime=file["mime"])


class PausingRetirementStorage(InMemoryStorageProvider):
    def __init__(self):
        self.started = Event()
        self.resume = Event()
        self.retired_keys = []

    def retire_file(self, owner, scope, key):
        self.retired_keys.append(key)
        self.started.set()
        assert self.resume.wait(30)
        raise OSError("temporary S3 failure")


class InterruptedUploadStorage(InMemoryStorageProvider):
    def put_fileobj(self, owner_scope, scope, key, fileobj, *, content_type="application/octet-stream"):
        super().put_fileobj(owner_scope, scope, key, fileobj, content_type=content_type)
        raise OSError("upload interrupted after writing the blob")


@contextmanager
def interrupted_upload():
    provider = InterruptedUploadStorage()
    install_storage_provider_builder_for_tests(lambda: provider)
    reset_storage_provider_for_tests()
    try:
        yield provider
    finally:
        install_storage_provider_builder_for_tests(InMemoryStorageProvider)
        reset_storage_provider_for_tests()


class BlockedS3Upload:
    def __init__(self):
        self.started = anyio.Event()

    async def put_object(self, **kwargs):
        self.started.set()
        await anyio.sleep_forever()


class BlockedFileDownload:
    @asynccontextmanager
    async def open(self, *args):
        await anyio.sleep_forever()
        yield


class FileConsumerRuntime:
    def __init__(self, *, drains):
        self.drains = drains
        self.running = False
        self.drain_called = False

    def start_if_needed(self, *args, **kwargs):
        self.running = True

    def drain(self, *, deadline_sec):
        self.drain_called = True
        if self.drains:
            self.running = False
        return self.drains

    async def start(self, *args, **kwargs):
        self.running = True

    async def stop(self):
        if not self.drain(deadline_sec=0):
            raise TimeoutError("consumer is still running")


class FileBlobDouble:
    def __init__(self):
        self.traces = []

    async def put(self, owner, scope, key, path, mime, *, size):
        self.traces.append(aef_tracing.current_trace_id())
        async with await anyio.open_file(path, "rb") as source:
            get_storage_provider().put_bytes(owner, scope, key, await source.read(), content_type=mime)

    @asynccontextmanager
    async def open(self, owner, scope, key):
        self.traces.append(aef_tracing.current_trace_id())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "content"
            get_storage_provider().copy_to_path(owner, scope, key, path, max_bytes=2**63 - 1)
            async with await anyio.open_file(path, "rb") as source:

                async def chunks():
                    while chunk := await source.read(256 * 1024):
                        yield chunk

                yield chunks()


@contextmanager
def file_client(tmp_path):
    @asynccontextmanager
    async def lifespan(app):
        async_engine_factory.initialize_beside_store(load_settings())
        initialize_transfer_limits(runtime_persistence.current.engine, load_settings(), reserve_outgoing=True)
        app.state.file_blob = FileBlobDouble()
        app.state.owner_history_repository = SqlOwnerHistoryRepository(
            runtime_persistence.current.engine, history_transactions=file_transaction
        )
        try:
            yield
        finally:
            await async_engine_factory.dispose()

    app = Starlette(routes=collect_routes(data_dir=tmp_path, repo_dir=tmp_path), lifespan=lifespan)
    with TestClient(app) as client:
        yield client
