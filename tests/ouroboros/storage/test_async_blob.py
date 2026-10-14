import asyncio
import builtins
import io
import os
from uuid import uuid4

import anyio
import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from ouroboros.observability import aef_tracing
from ouroboros.storage.blob.async_backend import AsyncBlob, open_file_blob
from ouroboros.storage.blob.s3_backend import S3StorageProvider
from ouroboros.storage.settings.store import load_settings
from tests.helpers.files import BlockedS3Upload
from tests.helpers.shared import TEST_SCOPE, scope_with


def test_cancelling_upload_closes_its_local_file_before_returning(tmp_path, monkeypatch):
    path = tmp_path / "content"
    path.write_bytes(b"hello")
    opened = []
    original = builtins.open

    def track(file, *args, **kwargs):
        handle = original(file, *args, **kwargs)
        if file == path:
            opened.append(handle)
        return handle

    monkeypatch.setattr(builtins, "open", track)

    async def scenario():
        client = BlockedS3Upload()
        async with anyio.create_task_group() as tasks:

            async def upload():
                await AsyncBlob(load_settings(), client).put(
                    TEST_SCOPE, "uploads", "cancelled", path, "text/plain", size=5
                )

            tasks.start_soon(upload)
            await client.started.wait()
            tasks.cancel_scope.cancel()
        assert opened and all(handle.closed for handle in opened)

    anyio.run(scenario)


@pytest.mark.integration
def test_native_s3_streaming_and_sync_bridge_preserve_immutable_keys(tmp_path, monkeypatch):
    endpoint = os.environ.get("TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("set TEST_S3_ENDPOINT to a disposable S3-compatible service")
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    settings = load_settings().model_copy(
        update={
            "OUROBOROS_S3_ENDPOINT": endpoint,
            "OUROBOROS_S3_BUCKET": "files-" + uuid4().hex,
            "OUROBOROS_S3_REGION": "us-east-1",
            "OUROBOROS_S3_ACCESS_KEY": SecretStr("ouroboros"),
            "OUROBOROS_S3_SECRET_KEY": SecretStr("ouroboros-test"),
        }
    )
    path = tmp_path / "large.txt"
    payload = b"x" * (9 * 1024 * 1024)
    path.write_bytes(payload)
    traces = []
    trace_id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    def record(request, **kwargs):
        traces.append(request.headers.get(aef_tracing.TRACE_HEADER_NAME))

    async def scenario():
        async with open_file_blob(settings) as blob:
            await blob.client.create_bucket(Bucket=settings.OUROBOROS_S3_BUCKET)
            blob.client.meta.events.register("before-send.s3.*", record)
            provider = S3StorageProvider(settings, file_client=blob.client, file_loop=asyncio.get_running_loop())
            try:
                await blob.put(TEST_SCOPE, "uploads", "large.txt", path, "text/plain", size=len(payload))
                async with blob.open(TEST_SCOPE, "uploads", "large.txt") as chunks:
                    assert b"".join([chunk async for chunk in chunks]) == payload
                with pytest.raises(ClientError):
                    async with blob.open(scope_with(TEST_SCOPE, owner_id="other"), "uploads", "large.txt"):
                        pytest.fail("cross-owner read")
                await asyncio.to_thread(provider.put_fileobj, TEST_SCOPE, "uploads", "small.txt", io.BytesIO(b"small"))
                async with blob.open(TEST_SCOPE, "uploads", "small.txt") as chunks:
                    assert b"".join([chunk async for chunk in chunks]) == b"small"
                await asyncio.to_thread(provider.retire_file, TEST_SCOPE, "uploads", "large.txt")
                with pytest.raises(ClientError) as rejected:
                    await blob.put(TEST_SCOPE, "uploads", "large.txt", path, "text/plain", size=len(payload))
                assert rejected.value.response["Error"]["Code"] == "PreconditionFailed"
                assert not (await blob.client.list_multipart_uploads(Bucket=settings.OUROBOROS_S3_BUCKET)).get(
                    "Uploads"
                )
                async with blob.open(TEST_SCOPE, "uploads", "large.txt") as chunks:
                    assert b"".join([chunk async for chunk in chunks]) == b""
            finally:
                provider.close()

    with aef_tracing.trace_scope(trace_id):
        asyncio.run(scenario())
    assert traces and set(traces) == {trace_id}
