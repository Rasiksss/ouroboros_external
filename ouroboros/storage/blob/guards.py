"""Production storage and database startup guards."""

from __future__ import annotations

from ouroboros.storage.blob.factory import resolve_storage_backend, storage_backend_name
from ouroboros.storage.settings.profile import is_production


class ProductionStorageConfigError(RuntimeError):
    """Raised when production deploy lacks required DB or object storage."""


def validate_production_storage() -> None:
    """Fail fast when production cannot persist state without PVC."""
    if not is_production():
        return

    from ouroboros.storage.db_url import collect_runtime_db_urls
    from ouroboros.storage.settings.store import load_settings

    settings = load_settings()
    db_urls = collect_runtime_db_urls(settings)
    if not db_urls or not any(str(url).strip().lower().startswith("postgresql") for url in db_urls):
        raise ProductionStorageConfigError(
            "OUROBOROS_RUNTIME_DB_URL (or OUROBOROS_RUNTIME_DB_URL_1..N) is required in production "
            "(use PostgreSQL; local runtime.sqlite3 is not durable without a persistent volume)."
        )

    backend = resolve_storage_backend()
    if backend != "s3":
        allow_file = str(settings.OUROBOROS_ALLOW_FILE_STORAGE_IN_PRODUCTION or "").strip().lower()
        if allow_file not in {"1", "true", "yes", "on"}:
            raise ProductionStorageConfigError(
                "OUROBOROS_STORAGE_BACKEND must be 's3' in production "
                "(set OUROBOROS_ALLOW_FILE_STORAGE_IN_PRODUCTION=true only for exceptional local prod tests)."
            )
        return

    bucket = str(settings.OUROBOROS_S3_BUCKET or "").strip()
    if not bucket:
        raise ProductionStorageConfigError(
            "OUROBOROS_S3_BUCKET is required when OUROBOROS_STORAGE_BACKEND=s3 in production."
        )


def storage_status_summary() -> dict[str, str]:
    return {
        "storage_backend": storage_backend_name(),
        "deploy_env": "production" if is_production() else "development",
    }
