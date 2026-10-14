from __future__ import annotations

from ouroboros.storage.blob.factory import STORAGE_BACKEND_NAME
from ouroboros.storage.settings.profile import is_production


class ProductionStorageConfigError(RuntimeError):
    pass


def validate_production_storage() -> None:
    from ouroboros.storage.settings.store import load_settings

    bucket = str(load_settings().OUROBOROS_S3_BUCKET or "").strip()
    if not bucket:
        raise ProductionStorageConfigError(
            "OUROBOROS_S3_BUCKET is required: object storage is S3 in every environment."
        )


def storage_status_summary() -> dict[str, str]:
    """What a deployment reports about its storage: the backend that holds durable objects."""
    return {
        "storage_backend": STORAGE_BACKEND_NAME,
        "deploy_env": "production" if is_production() else "development",
    }
