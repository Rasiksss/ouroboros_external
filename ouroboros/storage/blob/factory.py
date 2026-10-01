"""StorageProvider factory and backend resolution."""

from __future__ import annotations

import pathlib
from typing import Literal

from ouroboros.storage.blob.file_backend import FileStorageProvider
from ouroboros.storage.blob.protocol import StorageProvider
from ouroboros.storage.blob.scoped_backend import ScopedHybridProvider
from ouroboros.storage.settings.profile import get_deploy_env

StorageBackend = Literal["file", "s3"]

_PROVIDER: StorageProvider | None = None


def resolve_storage_backend() -> StorageBackend:
    from ouroboros.storage.settings.store import load_settings

    explicit = str(load_settings().OUROBOROS_STORAGE_BACKEND or "").strip().lower()
    if explicit in ("file", "s3"):
        return explicit  # type: ignore[return-value]
    if get_deploy_env() == "development":
        return "file"
    return "s3"


def create_storage_provider(
    *,
    data_dir: pathlib.Path | None = None,
    backend: StorageBackend | None = None,
) -> StorageProvider:
    resolved = backend or resolve_storage_backend()
    if resolved == "file":
        root = data_dir
        if root is None:
            from ouroboros.storage.settings.paths import DATA_DIR

            root = DATA_DIR
        return FileStorageProvider(pathlib.Path(root))

    from ouroboros.storage.blob.s3_backend import S3StorageProvider

    local_root = data_dir
    if local_root is None:
        from ouroboros.storage.settings.paths import DATA_DIR

        local_root = DATA_DIR
    local_root = pathlib.Path(local_root)
    from ouroboros.storage.settings.store import load_settings

    settings = load_settings()
    bucket = str(settings.OUROBOROS_S3_BUCKET or "").strip()
    remote = S3StorageProvider(
        bucket=bucket,
        region=str(settings.OUROBOROS_S3_REGION or "").strip(),
        endpoint_url=str(settings.OUROBOROS_S3_ENDPOINT or "").strip(),
        access_key=str(settings.OUROBOROS_S3_ACCESS_KEY.get_secret_value() or "").strip(),
        secret_key=str(settings.OUROBOROS_S3_SECRET_KEY.get_secret_value() or "").strip(),
    )
    return ScopedHybridProvider(remote=remote, local=FileStorageProvider(local_root))


def get_storage_provider(*, force_reload: bool = False) -> StorageProvider:
    global _PROVIDER
    if _PROVIDER is not None and not force_reload:
        return _PROVIDER
    _PROVIDER = create_storage_provider()
    return _PROVIDER


def reset_storage_provider_for_tests() -> None:
    global _PROVIDER
    _PROVIDER = None


def storage_backend_name() -> str:
    backend = resolve_storage_backend()
    if backend == "s3":
        return "s3+local_logs"
    return backend
