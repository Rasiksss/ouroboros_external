from __future__ import annotations

import io
from unittest.mock import Mock

import pytest

pytest.importorskip("moto")

from moto import mock_aws

from ouroboros.storage.settings.store import load_settings
from tests.helpers.shared import TEST_SCOPE


def test_s3_list_keys_stops_at_limit_and_forwards_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    from ouroboros.storage.blob.keys import owner_scope_prefix
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
    provider = S3StorageProvider(load_settings().model_copy(update={"OUROBOROS_S3_BUCKET": "bucket"}))

    assert provider.list_keys(
        TEST_SCOPE,
        "uploads",
        "_expiry/",
        limit=2,
        start_after="_expiry/previous.json",
    ) == ["_expiry/1.json", "_expiry/2.json"]
    first, second = (call.kwargs for call in client.list_objects_v2.call_args_list)
    assert first["MaxKeys"] == 2
    owner_prefix = owner_scope_prefix(TEST_SCOPE, "uploads")
    assert first["StartAfter"].endswith(f"/uploads/{owner_prefix}_expiry/previous.json")
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
        TEST_SCOPE,
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
        TEST_SCOPE,
        "uploads",
        "_meta/chat/id.json",
        {"value": "first"},
    )
    assert provider._client.delete_object.call_args.kwargs["IfMatch"] == '"scanned-etag"'


def test_s3_exists_propagates_non_not_found_errors() -> None:
    from ouroboros.storage.blob.s3_backend import S3StorageProvider

    provider = object.__new__(S3StorageProvider)
    provider.bucket = "bucket"
    provider._client = type(
        "FailingClient",
        (),
        {"head_object": lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("s3 unavailable"))},
    )()

    with pytest.raises(OSError, match="s3 unavailable"):
        provider.exists(TEST_SCOPE, "uploads", "chat/file.txt")


def test_s3_get_bytes_propagates_non_not_found_errors() -> None:
    from ouroboros.storage.blob.s3_backend import S3StorageProvider

    provider = object.__new__(S3StorageProvider)
    provider.bucket = "bucket"
    provider._client = type(
        "FailingClient",
        (),
        {"get_object": lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("s3 unavailable"))},
    )()

    with pytest.raises(OSError, match="s3 unavailable"):
        provider.get_bytes(TEST_SCOPE, "uploads", "chat/file.txt")


@mock_aws
def test_s3_create_json_if_absent_preserves_first_value(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    from ouroboros.storage.blob.s3_backend import S3StorageProvider

    bucket = "ouroboros-create-only"
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
    monkeypatch.setenv("OUROBOROS_STORAGE_TENANT_ID", "default")
    from tests.conftest import refresh_test_config

    refresh_test_config()
    provider = S3StorageProvider(
        load_settings().model_copy(update={"OUROBOROS_S3_BUCKET": bucket, "OUROBOROS_S3_REGION": "us-east-1"})
    )

    assert provider.create_json_if_absent(
        TEST_SCOPE,
        "uploads",
        "_meta/chat/id/file.json",
        {"value": "first"},
    )
    assert not provider.create_json_if_absent(
        TEST_SCOPE,
        "uploads",
        "_meta/chat/id/file.json",
        {"value": "second"},
    )
    assert provider.get_json(TEST_SCOPE, "uploads", "_meta/chat/id/file.json") == {"value": "first"}
    assert not provider.delete_json_if_matches(
        TEST_SCOPE,
        "uploads",
        "_meta/chat/id/file.json",
        {"value": "second"},
    )
    assert provider.replace_json_if_matches(
        TEST_SCOPE,
        "uploads",
        "_meta/chat/id/file.json",
        {"value": "first"},
        {"value": "replacement"},
    )
    assert provider.delete_json_if_matches(
        TEST_SCOPE,
        "uploads",
        "_meta/chat/id/file.json",
        {"value": "replacement"},
    )
    assert provider.get_json(TEST_SCOPE, "uploads", "_meta/chat/id/file.json") is None


@mock_aws
def test_s3_put_get_and_tenant_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    from ouroboros.storage.blob.keys import build_object_key
    from ouroboros.storage.blob.s3_backend import S3StorageProvider

    bucket = "ouroboros-test"
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
    monkeypatch.setenv("OUROBOROS_STORAGE_TENANT_ID", "default")
    from tests.conftest import refresh_test_config

    refresh_test_config()

    provider = S3StorageProvider(
        load_settings().model_copy(update={"OUROBOROS_S3_BUCKET": bucket, "OUROBOROS_S3_REGION": "us-east-1"})
    )
    provider.put_text(TEST_SCOPE, "memory", "identity.md", "s3-agent")
    assert provider.get_text(TEST_SCOPE, "memory", "identity.md") == "s3-agent"
    assert build_object_key(TEST_SCOPE, "memory", "identity.md") == "tenants/default/memory/identity.md"


@mock_aws
def test_s3_provider_conditional_json_writes_reject_stale_values(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    from ouroboros.storage.blob.s3_backend import S3StorageProvider
    from tests.conftest import refresh_test_config

    bucket = "ouroboros-cas-test"
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
    monkeypatch.setenv("OUROBOROS_STORAGE_TENANT_ID", "default")
    refresh_test_config()
    provider = S3StorageProvider(
        load_settings().model_copy(update={"OUROBOROS_S3_BUCKET": bucket, "OUROBOROS_S3_REGION": "us-east-1"})
    )
    key = "skills/tasks-scheduler/schedules/one.json"
    initial = {"revision": 1, "state": "pending"}
    updated = {"revision": 2, "state": "active"}

    assert provider.create_json_if_absent(TEST_SCOPE, "state", key, initial)
    assert not provider.create_json_if_absent(TEST_SCOPE, "state", key, {"revision": 99})
    assert not provider.replace_json_if_matches(TEST_SCOPE, "state", key, {"revision": 99}, updated)
    assert provider.replace_json_if_matches(TEST_SCOPE, "state", key, initial, updated)
    assert provider.get_json(TEST_SCOPE, "state", key) == updated


def test_production_guard_requires_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    from ouroboros.storage.blob.guards import ProductionStorageConfigError, validate_production_storage
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("OUROBOROS_RUNTIME_DB_URL_1", "postgresql+psycopg://u:p@db/ouroboros")
    monkeypatch.delenv("OUROBOROS_S3_BUCKET", raising=False)
    refresh_test_config()
    with pytest.raises(ProductionStorageConfigError, match="OUROBOROS_S3_BUCKET"):
        validate_production_storage()


def test_the_storage_guard_refuses_a_missing_bucket_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    from ouroboros.storage.blob.guards import ProductionStorageConfigError, validate_production_storage
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("OUROBOROS_ENV", "development")
    monkeypatch.delenv("OUROBOROS_MULTIUSER", raising=False)
    monkeypatch.delenv("OUROBOROS_S3_BUCKET", raising=False)
    refresh_test_config()

    with pytest.raises(ProductionStorageConfigError) as refusal:
        validate_production_storage()

    message = str(refusal.value)
    assert "OUROBOROS_S3_BUCKET" in message
    assert "production" not in message


def test_the_production_guard_makes_no_runtime_database_check_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect

    from ouroboros.storage.blob import guards
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("OUROBOROS_S3_BUCKET", "ouroboros-production")
    monkeypatch.delenv("OUROBOROS_RUNTIME_DB_URL", raising=False)
    monkeypatch.delenv("OUROBOROS_RUNTIME_DB_URL_1", raising=False)
    refresh_test_config()

    guards.validate_production_storage()

    source = inspect.getsource(guards)
    assert "collect_runtime_db_urls" not in source
    assert "OUROBOROS_RUNTIME_DB_URL" not in source
