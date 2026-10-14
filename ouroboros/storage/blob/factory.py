"""StorageProvider factory."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from ouroboros.storage.settings.schema import AppSettings

if TYPE_CHECKING:
    from types_aiobotocore_s3.client import S3Client

from ouroboros.storage.blob.protocol import StorageProvider

STORAGE_BACKEND_NAME = "s3"

_PROVIDER: StorageProvider | None = None
_BUILDER: Callable[[], StorageProvider] | None = None


def get_storage_provider() -> StorageProvider:
    global _PROVIDER
    if _PROVIDER is None:
        if _BUILDER is None:
            raise RuntimeError("object storage must be initialized before consumers")
        _PROVIDER = _BUILDER()
    return _PROVIDER


def initialize_storage_provider(
    settings: AppSettings,
    *,
    file_client: S3Client,
    file_loop: asyncio.AbstractEventLoop,
) -> None:
    global _PROVIDER
    # The S3 client imports tracing, whose startup reads storage configuration.
    from ouroboros.storage.blob.s3_backend import S3StorageProvider

    _PROVIDER = (
        _BUILDER()
        if _BUILDER is not None
        else S3StorageProvider(settings, file_client=file_client, file_loop=file_loop)
    )


def close_storage_provider() -> None:
    global _PROVIDER
    # Same startup cycle as initialization.
    from ouroboros.storage.blob.s3_backend import S3StorageProvider

    provider, _PROVIDER = _PROVIDER, None
    if isinstance(provider, S3StorageProvider):
        provider.close()


def reset_storage_provider_for_tests() -> None:
    global _PROVIDER
    _PROVIDER = None


def install_storage_provider_builder_for_tests(builder: Callable[[], StorageProvider] | None) -> None:
    global _BUILDER
    _BUILDER = builder
