"""S3StorageProvider tests with moto."""

from __future__ import annotations

import io
from unittest.mock import Mock

import pytest

pytest.importorskip("moto")

from moto import mock_aws


@pytest.fixture(autouse=True)
def _reset_provider() -> None:
    from ouroboros.storage.blob.factory import reset_storage_provider_for_tests

    reset_storage_provider_for_tests()
    yield
    reset_storage_provider_for_tests()


def test_s3_list_keys_stops_at_limit_and_forwards_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    from ouroboros.storage.blob.s3_backend import S3StorageProvider

    client = Mock()

    def _page(**kwargs):
        page = client.list_objects_v2.call_count
        return {
            "Contents": [{"Key": f"{kwargs['Prefix']}{page}.json"}],
            "IsTruncated": page < 3,
            "NextContinuationToken": f"page-{page + 1}",
        }

    client.list_objects_v2.side_effect = _page
    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: client)
    provider = S3StorageProvider(bucket="bucket")

    assert provider.list_keys(
        "uploads",
        "_expiry/",
        limit=2,
        start_after="_expiry/previous.json",
    ) == ["_expiry/1.json", "_expiry/2.json"]
    first, second = (call.kwargs for call in client.list_objects_v2.call_args_list)
    assert first["MaxKeys"] == 2
    assert first["StartAfter"].endswith("/uploads/_expiry/previous.json")
    assert "ContinuationToken" not in first
    assert second["MaxKeys"] == 1
    assert second["ContinuationToken"] == "page-2"


def test_s3_create_json_if_absent_retries_conditional_conflict() -> None:
    from ouroboros.storage.blob.s3_backend import S3StorageProvider

    def error(code: str, status: int) -> RuntimeError:
        failure = RuntimeError(code)
        failure.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }
        return failure

    provider = object.__new__(S3StorageProvider)
    provider.bucket = "bucket"
    provider._client = Mock()
    provider._client.put_object.side_effect = [
        error("ConditionalRequestConflict", 409),
        error("PreconditionFailed", 412),
    ]

    assert not provider.create_json_if_absent(
        "uploads",
        "_meta/chat/id.json",
        {"value": "second"},
    )
    assert provider._client.put_object.call_count == 2


def test_s3_conditional_json_delete_uses_scanned_etag() -> None:
    from ouroboros.storage.blob.s3_backend import S3StorageProvider

    failure = RuntimeError("PreconditionFailed")
    failure.response = {
        "Error": {"Code": "PreconditionFailed"},
        "ResponseMetadata": {"HTTPStatusCode": 412},
    }
    provider = object.__new__(S3StorageProvider)
    provider.bucket = "bucket"
    provider._client = Mock()
    provider._client.get_object.return_value = {
        "Body": io.BytesIO(b'{"value": "first"}'),
        "ETag": '"scanned-etag"',
    }
    provider._client.delete_object.side_effect = failure

    assert not provider.delete_json_if_matches(
        "uploads",
        "_meta/chat/id.json",
        {"value": "first"},
    )
    assert provider._client.delete_object.call_args.kwargs["IfMatch"] == '"scanned-etag"'


@mock_aws
def test_s3_create_json_if_absent_preserves_first_value(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import boto3

    from ouroboros.storage.blob.factory import create_storage_provider

    bucket = "ouroboros-create-only"
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_S3_BUCKET", bucket)
    monkeypatch.setenv("OUROBOROS_S3_REGION", "us-east-1")
    monkeypatch.setenv("OUROBOROS_STORAGE_TENANT_ID", "default")
    from tests.conftest import refresh_test_config

    refresh_test_config()
    provider = create_storage_provider(backend="s3")

    assert provider.create_json_if_absent(
        "uploads",
        "_meta/chat/id/file.json",
        {"value": "first"},
    )
    assert not provider.create_json_if_absent(
        "uploads",
        "_meta/chat/id/file.json",
        {"value": "second"},
    )
    assert provider.get_json("uploads", "_meta/chat/id/file.json") == {"value": "first"}
    assert not provider.delete_json_if_matches(
        "uploads",
        "_meta/chat/id/file.json",
        {"value": "second"},
    )
    assert provider.replace_json_if_matches(
        "uploads",
        "_meta/chat/id/file.json",
        {"value": "first"},
        {"value": "replacement"},
    )
    assert provider.delete_json_if_matches(
        "uploads",
        "_meta/chat/id/file.json",
        {"value": "replacement"},
    )
    assert provider.get_json("uploads", "_meta/chat/id/file.json") is None


@mock_aws
def test_s3_put_get_and_tenant_prefix(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    from ouroboros.storage.blob.factory import create_storage_provider
    from ouroboros.storage.blob.keys import build_object_key

    bucket = "ouroboros-test"
    data_dir = tmp_path / "ouroboros_data"
    data_dir.mkdir(exist_ok=True)
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OUROBOROS_S3_BUCKET", bucket)
    monkeypatch.setenv("OUROBOROS_S3_REGION", "us-east-1")
    monkeypatch.setenv("OUROBOROS_STORAGE_TENANT_ID", "default")
    from tests.conftest import refresh_test_config

    refresh_test_config()

    provider = create_storage_provider(backend="s3")
    provider.put_text("memory", "identity.md", "s3-agent")
    assert provider.get_text("memory", "identity.md") == "s3-agent"
    provider.put_fileobj(
        "uploads",
        "chat/test/upload.bin",
        io.BytesIO(b"streamed"),
        content_type="application/octet-stream",
    )
    assert provider.get_bytes("uploads", "chat/test/upload.bin") == b"streamed"
    assert build_object_key("memory", "identity.md") == "tenants/default/memory/identity.md"

    assert provider.append_event("logs", "custom", {"type": "ping"}) is True
    assert provider.list_keys("logs", "custom/events/") == []


@mock_aws
def test_s3_provider_conditional_json_writes_reject_stale_values(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    from ouroboros.storage.blob.factory import create_storage_provider
    from tests.conftest import refresh_test_config

    bucket = "ouroboros-cas-test"
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_S3_BUCKET", bucket)
    monkeypatch.setenv("OUROBOROS_S3_REGION", "us-east-1")
    monkeypatch.setenv("OUROBOROS_STORAGE_TENANT_ID", "default")
    refresh_test_config()
    provider = create_storage_provider(data_dir=tmp_path, backend="s3")
    key = "skills/tasks-scheduler/schedules/one.json"
    initial = {"revision": 1, "state": "pending"}
    updated = {"revision": 2, "state": "active"}

    assert provider.create_json_if_absent("state", key, initial)
    assert not provider.create_json_if_absent("state", key, {"revision": 99})
    assert not provider.replace_json_if_matches("state", key, {"revision": 99}, updated)
    assert provider.replace_json_if_matches("state", key, initial, updated)
    assert provider.get_json("state", key) == updated


@mock_aws
def test_production_guard_requires_db_and_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    from ouroboros.storage.blob.guards import ProductionStorageConfigError, validate_production_storage
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.delenv("OUROBOROS_RUNTIME_DB_URL", raising=False)
    refresh_test_config()
    with pytest.raises(ProductionStorageConfigError, match="OUROBOROS_RUNTIME_DB_URL"):
        validate_production_storage()

    monkeypatch.setenv("OUROBOROS_RUNTIME_DB_URL", "postgresql+psycopg://u:p@db/ouroboros")
    monkeypatch.setenv("OUROBOROS_STORAGE_BACKEND", "s3")
    monkeypatch.delenv("OUROBOROS_S3_BUCKET", raising=False)
    refresh_test_config()
    with pytest.raises(ProductionStorageConfigError, match="OUROBOROS_S3_BUCKET"):
        validate_production_storage()
