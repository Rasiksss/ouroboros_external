"""Hybrid storage: mutable event streams use SQL; append-only logs stay local."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("moto")

from moto import mock_aws


@pytest.fixture(autouse=True)
def _reset_provider() -> None:
    from ouroboros.storage.blob.factory import reset_storage_provider_for_tests

    reset_storage_provider_for_tests()
    yield
    reset_storage_provider_for_tests()


@mock_aws
def test_record_event_events_stream_uses_runtime_db_not_logs_with_s3(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    from ouroboros.observability.event_record import record_event
    from ouroboros.storage.blob.factory import create_storage_provider
    from ouroboros.storage.blob.keys import build_scope_prefix
    from ouroboros.storage.runtime_store import get_runtime_store

    bucket = "ouroboros-test"
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
    monkeypatch.setenv("OUROBOROS_S3_BUCKET", bucket)
    monkeypatch.setenv("OUROBOROS_S3_REGION", "us-east-1")
    monkeypatch.setenv("OUROBOROS_STORAGE_TENANT_ID", "default")

    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    from tests.conftest import refresh_test_config

    refresh_test_config()
    from ouroboros.storage.blob.factory import get_storage_provider

    create_storage_provider(data_dir=tmp_path, backend="s3")
    get_storage_provider(force_reload=True)
    record_event(tmp_path, "events", "sample_event", {"task_id": "t1", "value": 7})

    log_path = tmp_path / "logs" / "events.jsonl"
    assert not log_path.exists()
    store = get_runtime_store(tmp_path)
    assert store is not None
    stored = store.list_audit_events(log_stream="events", limit=1)[0].metadata
    assert stored["type"] == "sample_event"
    assert stored["log_stream"] == "events"
    assert stored["log_kind"] == "runtime_event"

    s3 = boto3.client("s3", region_name="us-east-1")
    prefix = build_scope_prefix("logs")
    response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    assert not response.get("Contents")


@mock_aws
def test_memory_journal_append_local_with_s3_backend(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    from ouroboros.utils import append_jsonl

    bucket = "ouroboros-test"
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
    monkeypatch.setenv("OUROBOROS_S3_BUCKET", bucket)
    monkeypatch.setenv("OUROBOROS_S3_REGION", "us-east-1")

    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    from tests.conftest import refresh_test_config

    refresh_test_config()
    from ouroboros.storage.blob.factory import create_storage_provider, reset_storage_provider_for_tests

    reset_storage_provider_for_tests()
    create_storage_provider(data_dir=tmp_path, backend="s3")

    # Any ``memory/*_journal.jsonl`` is routed to disk; no such file is written
    # by the application today, so the rule is exercised with a probe name.
    journal = tmp_path / "memory" / "probe_journal.jsonl"
    ok = append_jsonl(journal, {"type": "block_appended", "content_len": 42})
    assert ok is True
    assert journal.is_file()
    line = json.loads(journal.read_text(encoding="utf-8").strip())
    assert line["content_len"] == 42
