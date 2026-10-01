"""Object storage providers (file / S3) for durable non-DB agent data."""

from ouroboros.storage.blob.drive import DriveStorage, get_drive_storage, state_write_lock
from ouroboros.storage.blob.factory import (
    create_storage_provider,
    get_storage_provider,
    reset_storage_provider_for_tests,
    resolve_storage_backend,
    storage_backend_name,
)
from ouroboros.storage.blob.guards import ProductionStorageConfigError, validate_production_storage
from ouroboros.storage.blob.protocol import StorageProvider

__all__ = [
    "DriveStorage",
    "ProductionStorageConfigError",
    "StorageProvider",
    "create_storage_provider",
    "get_drive_storage",
    "get_storage_provider",
    "reset_storage_provider_for_tests",
    "resolve_storage_backend",
    "state_write_lock",
    "storage_backend_name",
    "validate_production_storage",
]
