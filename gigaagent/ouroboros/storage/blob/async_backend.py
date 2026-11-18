from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from new_core.identity import OwnerScope
from ouroboros.storage.blob.keys import build_object_key
from ouroboros.storage.blob.s3_backend import open_s3_client, put_s3_fileobj
from ouroboros.storage.settings.schema import AppSettings

if TYPE_CHECKING:
    from types_aiobotocore_s3.client import S3Client

CHUNK_BYTES = 256 * 1024


@asynccontextmanager
async def open_file_blob(settings: AppSettings) -> AsyncIterator[AsyncBlob]:
    async with open_s3_client(settings) as client:
        yield AsyncBlob(settings, client)


class AsyncBlob:
    def __init__(self, settings: AppSettings, client: S3Client) -> None:
        self.settings, self.client = settings, client

    async def put(self, owner: OwnerScope, scope: str, key: str, path: Path, mime: str, *, size: int) -> None:
        source = await anyio.open_file(path, "rb")
        try:
            await put_s3_fileobj(
                self.client, self.settings, build_object_key(owner, scope, key), mime, source, size=size
            )
        finally:
            with anyio.CancelScope(shield=True):
                await source.aclose()

    @asynccontextmanager
    async def open(self, owner: OwnerScope, scope: str, key: str) -> AsyncIterator[AsyncIterator[bytes]]:
        async with asyncio.timeout(self.settings.OUROBOROS_FILE_IO_TIMEOUT_SEC):
            response = await self.client.get_object(
                Bucket=self.settings.OUROBOROS_S3_BUCKET, Key=build_object_key(owner, scope, key)
            )
            async with response["Body"] as body:
                yield body.iter_chunks(CHUNK_BYTES)
