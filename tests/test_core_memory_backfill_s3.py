"""Migration 0006 on the S3 backend: the key it addresses, and what it does on failure.

The migration reads legacy blobs through readers defined inside its own module,
so these tests inject failure at the boto3 boundary — the shared surface the
reader actually crosses. Alembic executes the migration file as a module object
of its own, so patching the importable copy would not reach the running code;
``_migration()`` below is only used for the key-layout assertions, which need
the class rather than a live run.
"""

from __future__ import annotations

import importlib
import json
import pathlib

import pytest
from sqlalchemy import create_engine, inspect, text

pytest.importorskip("moto")

from moto import mock_aws

from ouroboros.storage.migrations.runner import upgrade_head
from ouroboros.storage.runtime_factory import sqlite_url_for_path

BUCKET = "ouroboros-backfill-test"


def _migration():
    return importlib.import_module("ouroboros.storage.migrations.versions.0006_core_memory")


@pytest.fixture(autouse=True)
def _reset_provider() -> None:
    from ouroboros.storage.blob.factory import reset_storage_provider_for_tests

    reset_storage_provider_for_tests()
    yield
    reset_storage_provider_for_tests()


def _setup_s3(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    """Point the app at a moto bucket and return a client for seeding it."""
    import boto3

    from tests.conftest import refresh_test_config

    data_dir = tmp_path / "ouroboros_data"
    data_dir.mkdir(exist_ok=True)
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OUROBOROS_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("OUROBOROS_S3_BUCKET", BUCKET)
    monkeypatch.setenv("OUROBOROS_S3_REGION", "us-east-1")
    refresh_test_config()
    return client


def _put(client, key: str, body: str) -> None:
    from ouroboros.storage.blob.keys import build_object_key

    client.put_object(Bucket=BUCKET, Key=build_object_key("memory", key), Body=body.encode("utf-8"))


def _seed_legacy_blobs(client) -> None:
    _put(
        client,
        "global/_index.json",
        json.dumps(
            {
                "entries": [
                    {
                        "name": "user_role",
                        "type": "user",
                        "description": "Who the user is",
                        "updated_at": 1_700_000_100.0,
                        "first_recorded_at": 1_700_000_000.0,
                        "status": "active",
                        "source": "distilled",
                        "recall_count": 3,
                    }
                ]
            }
        ),
    )
    _put(client, "global/user_role.md", "---\nname: user_role\ntype: user\n---\n\nSenior engineer.\n")
    _put(
        client,
        "scratchpad_blocks.json",
        json.dumps([{"ts": "2026-07-01T10:00:00+00:00", "source": "task", "content": "live block"}]),
    )
    _put(client, "knowledge/patterns.md", "# Patterns\n\nREVIEW_BLOCKED means the plan changed\n")
    _put(client, "identity.md", "# Who I Am\n\nSeeded on the bucket.\n")


def _upgraded_engine(tmp_path: pathlib.Path):
    engine = create_engine(sqlite_url_for_path(tmp_path / "state" / "runtime.sqlite3"))
    upgrade_head(engine)
    return engine


def _fail_boto3_client(monkeypatch, *, get_suffix: str = "", fail_list: bool = False) -> None:
    """Wrap the moto client so one operation returns a non-404 error."""
    import boto3
    from botocore.exceptions import ClientError

    real_client = boto3.client

    def _error(operation: str) -> ClientError:
        return ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, operation)

    class _Wrapper:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def get_object(self, **kwargs):
            if get_suffix and str(kwargs.get("Key", "")).endswith(get_suffix):
                raise _error("GetObject")
            return self._inner.get_object(**kwargs)

        def list_objects_v2(self, **kwargs):
            if fail_list:
                raise _error("ListObjectsV2")
            return self._inner.list_objects_v2(**kwargs)

    def _patched(service_name, *args, **kwargs):
        inner = real_client(service_name, *args, **kwargs)
        return _Wrapper(inner) if service_name == "s3" else inner

    monkeypatch.setattr(boto3, "client", _patched)


# ------------------------------------------------------------------- key layout


@mock_aws
@pytest.mark.parametrize(
    "key",
    ["global/_index.json", "global/user_role.md", "scratchpad_blocks.json", "knowledge/patterns.md", "identity.md"],
)
def test_the_migrations_object_key_equals_the_shared_key_builder(key, tmp_path, monkeypatch):
    """A wrong prefix lists nothing and the backfill 'succeeds' with zero rows — pin it."""
    from ouroboros.storage.blob.keys import build_object_key

    _setup_s3(monkeypatch, tmp_path)
    reader = _migration()._S3BlobReader(bucket=BUCKET)

    assert reader.object_key(key) == build_object_key("memory", key)


@mock_aws
def test_the_key_layout_agrees_under_a_custom_tenant_and_prefix(tmp_path, monkeypatch):
    from ouroboros.storage.blob.keys import build_object_key

    _setup_s3(monkeypatch, tmp_path)
    monkeypatch.setenv("OUROBOROS_STORAGE_TENANT_ID", "acme")
    monkeypatch.setenv("OUROBOROS_S3_PREFIX", "/nested/env/")
    reader = _migration()._S3BlobReader(bucket=BUCKET)

    expected = build_object_key("memory", "global/user_role.md")
    assert reader.object_key("global/user_role.md") == expected
    assert expected.startswith("tenants/acme/nested/env/memory/")


def test_the_listing_reads_every_page(monkeypatch):
    """A truncated first page must not be mistaken for the whole prefix."""
    pages = [
        {
            "Contents": [{"Key": "tenants/default/memory/knowledge/a.md"}],
            "IsTruncated": True,
            "NextContinuationToken": "t1",
        },
        {"Contents": [{"Key": "tenants/default/memory/knowledge/b.md"}], "IsTruncated": False},
    ]
    seen: list[str | None] = []

    class _PagedClient:
        def list_objects_v2(self, **kwargs):
            seen.append(kwargs.get("ContinuationToken"))
            return pages[len(seen) - 1]

    reader = _migration()._S3BlobReader(bucket=BUCKET)
    reader._client_cache = _PagedClient()
    monkeypatch.setattr(type(reader), "scope_prefix", staticmethod(lambda: "tenants/default/memory/"))

    assert reader.list_keys("knowledge/") == ["knowledge/a.md", "knowledge/b.md"]
    assert seen == [None, "t1"]


# --------------------------------------------------------------------- backfill


@mock_aws
def test_the_backfill_copies_every_blob_out_of_the_bucket(tmp_path, monkeypatch):
    client = _setup_s3(monkeypatch, tmp_path)
    _seed_legacy_blobs(client)

    engine = _upgraded_engine(tmp_path)

    with engine.connect() as conn:
        rows = {r["name"]: dict(r) for r in conn.execute(text("SELECT * FROM memory")).mappings()}
        blocks = [dict(r) for r in conn.execute(text("SELECT * FROM scratchpad_blocks")).mappings()]

    # One table, three sources, each row carrying the kind its source implies.
    assert {name: row["kind"] for name, row in rows.items()} == {
        "user_role": "user",
        "patterns": "knowledge",
        "identity": "identity",
    }
    assert rows["user_role"]["content"].strip() == "Senior engineer."
    assert rows["user_role"]["source"] == "distilled"
    assert rows["user_role"]["recall_count"] == 3
    assert rows["patterns"]["description"] == "REVIEW_BLOCKED means the plan changed"
    # ``patterns`` is an ordinary knowledge row now — asserted by kind above.
    assert rows["identity"]["content"] == "# Who I Am\n\nSeeded on the bucket.\n"
    assert [b["content"] for b in blocks] == ["live block"]
    assert [b["status"] for b in blocks] == ["active"]


@mock_aws
def test_no_local_file_is_read_or_written_on_the_s3_backend(tmp_path, monkeypatch):
    """Shadow files on pod disk are not a source; the bucket is."""
    client = _setup_s3(monkeypatch, tmp_path)
    _seed_legacy_blobs(client)
    shadow = tmp_path / "ouroboros_data" / "memory" / "global"
    shadow.mkdir(parents=True, exist_ok=True)
    (shadow / "shadow_fact.md").write_text("---\nname: shadow_fact\n---\n\nlocal only\n", encoding="utf-8")

    engine = _upgraded_engine(tmp_path)

    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(text("SELECT name FROM memory"))}
    assert names == {"user_role", "patterns", "identity"}


@mock_aws
def test_an_absent_object_is_tolerated_and_contributes_no_row(tmp_path, monkeypatch):
    client = _setup_s3(monkeypatch, tmp_path)
    _seed_legacy_blobs(client)
    _put(
        client,
        "global/_index.json",
        json.dumps(
            {"entries": [{"name": "user_role", "type": "user", "updated_at": 1.0}, {"name": "deleted", "type": "user"}]}
        ),
    )

    engine = _upgraded_engine(tmp_path)

    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(text("SELECT name FROM memory"))}
    assert names == {"user_role", "patterns", "identity"}


@mock_aws
def test_a_fresh_bucket_migrates_cleanly(tmp_path, monkeypatch):
    _setup_s3(monkeypatch, tmp_path)

    engine = _upgraded_engine(tmp_path)

    with engine.connect() as conn:
        # Only the seeded identity row: an empty bucket has nothing else to copy.
        names = [r[0] for r in conn.execute(text("SELECT name FROM memory"))]
        assert names == ["identity"]
        assert conn.execute(text("SELECT COUNT(*) FROM scratchpad_blocks")).scalar() == 0


# ------------------------------------------------------------------- fail closed


@mock_aws
def test_a_non_404_fetch_error_aborts_the_migration(tmp_path, monkeypatch):
    client = _setup_s3(monkeypatch, tmp_path)
    _seed_legacy_blobs(client)
    _fail_boto3_client(monkeypatch, get_suffix="knowledge/patterns.md")

    with pytest.raises(Exception, match="AccessDenied"):
        _upgraded_engine(tmp_path)


@mock_aws
def test_a_listing_failure_aborts_rather_than_reading_as_an_empty_prefix(tmp_path, monkeypatch):
    client = _setup_s3(monkeypatch, tmp_path)
    _seed_legacy_blobs(client)
    _fail_boto3_client(monkeypatch, fail_list=True)

    with pytest.raises(Exception, match="AccessDenied"):
        _upgraded_engine(tmp_path)


@mock_aws
def test_an_unreadable_manifest_aborts_rather_than_rebuilding_from_frontmatter(tmp_path, monkeypatch):
    client = _setup_s3(monkeypatch, tmp_path)
    _seed_legacy_blobs(client)
    _fail_boto3_client(monkeypatch, get_suffix="global/_index.json")

    with pytest.raises(Exception, match="AccessDenied"):
        _upgraded_engine(tmp_path)


@mock_aws
def test_a_failed_body_fetch_never_becomes_a_row_with_empty_content(tmp_path, monkeypatch):
    """The manifest entry is truthy, so the pre-change code inserted content=''."""
    client = _setup_s3(monkeypatch, tmp_path)
    _seed_legacy_blobs(client)
    _fail_boto3_client(monkeypatch, get_suffix="global/user_role.md")

    with pytest.raises(Exception, match="AccessDenied"):
        _upgraded_engine(tmp_path)

    engine = create_engine(sqlite_url_for_path(tmp_path / "state" / "runtime.sqlite3"))
    assert "memory" not in inspect(engine).get_table_names()


@mock_aws
def test_a_malformed_manifest_aborts_the_migration(tmp_path, monkeypatch):
    client = _setup_s3(monkeypatch, tmp_path)
    _seed_legacy_blobs(client)
    _put(client, "global/_index.json", "NOT VALID JSON {{{")

    with pytest.raises(Exception, match="(?i)json|expecting"):
        _upgraded_engine(tmp_path)


@mock_aws
@pytest.mark.parametrize("key", ["global/_index.json", "scratchpad_blocks.json"])
def test_an_empty_object_aborts_the_migration(tmp_path, monkeypatch, key):
    """A zero-byte object is a truncated write, and S3 is where that happens.

    ``get_text`` answers ``""`` for it and ``None`` only for a 404, so the two
    are distinguishable — and only the 404 is absence. Reading the empty object
    as absence stamps the revision over a manifest that lost every counter, or
    over a scratchpad that lost every block.
    """
    client = _setup_s3(monkeypatch, tmp_path)
    _seed_legacy_blobs(client)
    _put(client, key, "")

    with pytest.raises(ValueError, match="exists but is empty"):
        _upgraded_engine(tmp_path)

    engine = create_engine(sqlite_url_for_path(tmp_path / "state" / "runtime.sqlite3"))
    assert "memory" not in inspect(engine).get_table_names()
