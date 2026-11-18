"""Object storage provider for durable non-DB agent data."""

from ouroboros.storage.blob.drive import DriveStorage, get_drive_storage, state_write_lock
from ouroboros.storage.blob.factory import (
    get_storage_provider,
    reset_storage_provider_for_tests,
)
from ouroboros.storage.blob.guards import ProductionStorageConfigError, validate_production_storage
from ouroboros.storage.blob.protocol import StorageProvider

__all__ = [
    "DriveStorage",
    "ProductionStorageConfigError",
    "StorageProvider",
    "get_drive_storage",
    "get_storage_provider",
    "reset_storage_provider_for_tests",
    "state_write_lock",
    "validate_production_storage",
]
