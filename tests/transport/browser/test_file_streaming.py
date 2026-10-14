import asyncio
from builtins import ExceptionGroup
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text
from starlette.requests import Request

from infra.async_engine import async_engine_factory
from ouroboros.storage.blob.async_backend import AsyncBlob
from ouroboros.storage.file_catalog import FileCatalog
from ouroboros.storage.file_transfers import TransferLimits, get_transfer_limits, initialize_transfer_limits
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.store import load_settings
from tests.helpers.files import FileBlobDouble, stored_file
from tests.helpers.shared import TEST_SCOPE
from transport.browser.files import FileContent, api_file_upload


class TransferClock:
    def __init__(self):
        self.now = 0
        self.active = {}
        self.timeout = asyncio.timeout

    @asynccontextmanager
    async def budget(self, seconds):
        async with self.timeout(None) as timer:
            self.active[timer] = self.now + seconds
            try:
                yield timer
            finally:
                self.active.pop(timer)

    async def advance(self, seconds):
        self.now += seconds
        for timer, deadline in list(self.active.items()):
            if deadline <= self.now:
                timer.reschedule(asyncio.get_running_loop().time() - 1)
        await asyncio.sleep(0)


def active_transfers():
    with runtime_persistence.current.engine.connect() as conn:
        return conn.scalar(text("SELECT count(*) FROM file_transfer_leases"))


@pytest.mark.parametrize("idle", [False, True])
def test_multipart_progress_and_idle_deadline_do_not_hold_s3_slot(monkeypatch, idle):
    clock = TransferClock()
    monkeypatch.setattr(asyncio, "timeout", clock.budget)
    multipart = httpx.Request("POST", "http://test/api/files", files={"file": ("report.txt", b"hello", "text/plain")})
    body = multipart.read()
    chunks = [body[start : start + 40] for start in range(0, len(body), 40)]
    observed = []

    async def scenario():
        async_engine_factory.initialize_beside_store(load_settings())

        async def receive():
            if not chunks:
                await asyncio.Event().wait()
            observed.append(active_transfers())
            await clock.advance(121 if idle else 60)
            chunk = chunks.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/files",
            "query_string": b"",
            "headers": [(key.lower(), value) for key, value in multipart.headers.raw],
            "app": SimpleNamespace(state=SimpleNamespace(file_blob=FileBlobDouble())),
        }
        try:
            response = await api_file_upload(Request(scope, receive))
            assert response.status_code == (503 if idle else 201)
            assert observed and not any(observed)
            assert clock.now > 120
        finally:
            await async_engine_factory.dispose()

    asyncio.run(scenario())


def test_s3_download_finishes_within_lease_before_a_slow_progressing_client(monkeypatch):
    file = stored_file(data=b"hello")
    with runtime_persistence.current.engine.begin() as conn:
        record = FileCatalog(conn, TEST_SCOPE).file(file["id"])
    clock = TransferClock()
    monkeypatch.setattr(asyncio, "timeout", clock.budget)
    sent, leases = [], []

    class Body:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def iter_chunks(self, size):
            for chunk in (b"h", b"e", b"l", b"l", b"o"):
                assert active_transfers() == 1
                yield chunk

    class S3:
        async def get_object(self, **kwargs):
            return {"Body": Body()}

    async def scenario():
        async_engine_factory.initialize_beside_store(load_settings())

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            leases.append(active_transfers())
            await clock.advance(60)
            sent.append(message)
            with get_transfer_limits().slot(TEST_SCOPE, outgoing=True):
                pass

        try:
            response = FileContent(AsyncBlob(load_settings(), S3()), TEST_SCOPE, record, False)
            await response({"type": "http"}, receive, send)
            assert b"".join(item.get("body", b"") for item in sent) == b"hello"
            assert leases and not any(leases)
            assert clock.now > 120
        finally:
            await async_engine_factory.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("stalled_s3", [False, True])
def test_download_deadlines_release_resources_without_a_second_response(monkeypatch, stalled_s3):
    file = stored_file()
    with runtime_persistence.current.engine.begin() as conn:
        record = FileCatalog(conn, TEST_SCOPE).file(file["id"])
    clock = TransferClock()
    monkeypatch.setattr(asyncio, "timeout", clock.budget)
    closed, sent = [], []

    class Blob:
        @asynccontextmanager
        async def open(self, *args):
            async def chunks():
                if stalled_s3:
                    await clock.advance(121)
                yield b"hello"

            try:
                yield chunks()
            finally:
                closed.append(True)

    async def scenario():
        async_engine_factory.initialize_beside_store(load_settings())

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            sent.append(message)
            if not stalled_s3 and message["type"] == "http.response.body":
                await clock.advance(121)

        try:
            response = FileContent(Blob(), TEST_SCOPE, record, False)
            if stalled_s3:
                await response({"type": "http"}, receive, send)
            else:
                with pytest.raises(ExceptionGroup):
                    await response({"type": "http"}, receive, send)
            assert [message["status"] for message in sent if message["type"] == "http.response.start"] == [
                503 if stalled_s3 else 200
            ]
            assert closed and active_transfers() == 0
        finally:
            await async_engine_factory.dispose()

    asyncio.run(scenario())


def test_upload_and_multiple_previews_leave_agent_delivery_capacity(monkeypatch):
    monkeypatch.setattr("ouroboros.storage.file_transfers._ADMISSION_RETRY_INTERVAL_SEC", 0.001)
    file = stored_file()
    store = runtime_persistence.current
    with store.engine.begin() as conn:
        record = FileCatalog(conn, TEST_SCOPE).file(file["id"])
    multipart = httpx.Request("POST", "http://test/api/files", files={"file": ("new.txt", b"new", "text/plain")})
    payload = multipart.read()

    async def scenario():
        async_engine_factory.initialize_beside_store(load_settings())
        initialize_transfer_limits(store.engine, load_settings(), reserve_outgoing=True)
        uploading, uploaded, reading, resume = (asyncio.Event() for _ in range(4))

        class Blob(FileBlobDouble):
            @asynccontextmanager
            async def open(self, *args):
                reading.set()
                await resume.wait()
                async with super().open(*args) as chunks:
                    yield chunks

        async def receive_upload():
            if uploading.is_set():
                await asyncio.Event().wait()
            uploading.set()
            await uploaded.wait()
            return {"type": "http.request", "body": payload, "more_body": False}

        async def preview():
            messages = []

            async def receive():
                await asyncio.Event().wait()

            async def send(message):
                messages.append(message)

            await FileContent(Blob(), TEST_SCOPE, record, False)({"type": "http"}, receive, send)
            assert b"".join(message.get("body", b"") for message in messages) == b"hello"

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/files",
            "query_string": b"",
            "headers": [(key.lower(), value) for key, value in multipart.headers.raw],
            "app": SimpleNamespace(state=SimpleNamespace(file_blob=FileBlobDouble())),
        }
        try:
            async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
                upload = tasks.create_task(api_file_upload(Request(scope, receive_upload)))
                await uploading.wait()
                previews = [tasks.create_task(preview()) for _ in range(3)]
                await reading.wait()
                assert active_transfers() == 1
                agent = TransferLimits(store.engine, 8, 2, process_id="agent-replica", lease_seconds=420)
                async with agent.async_slot(TEST_SCOPE, async_engine_factory.engine, outgoing=True):
                    assert active_transfers() == 2
                resume.set()
                await asyncio.gather(*previews)
                uploaded.set()
            assert upload.result().status_code == 201
            assert active_transfers() == 0
        finally:
            await async_engine_factory.dispose()

    asyncio.run(scenario())
