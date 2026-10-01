"""FileStorageProvider roundtrip tests."""

from __future__ import annotations

import io
import os
import pathlib
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ouroboros.storage.blob import file_backend
from ouroboros.storage.blob.factory import create_storage_provider, reset_storage_provider_for_tests
from ouroboros.storage.blob.scopes import SCOPE_MEMORY, SCOPE_STATE


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
        provider.exists("uploads", "chat/file.txt")


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
        provider.get_bytes("uploads", "chat/file.txt")


@pytest.fixture(autouse=True)
def _reset_provider() -> None:
    reset_storage_provider_for_tests()
    yield
    reset_storage_provider_for_tests()


def test_file_put_get_text_and_json(tmp_path: pathlib.Path) -> None:
    provider = create_storage_provider(data_dir=tmp_path, backend="file")
    provider.put_text(SCOPE_MEMORY, "identity.md", "# Agent\n")
    assert provider.get_text(SCOPE_MEMORY, "identity.md") == "# Agent\n"

    provider.put_json(SCOPE_STATE, "skills/demo/enabled.json", {"enabled": True})
    loaded = provider.get_json(SCOPE_STATE, "skills/demo/enabled.json")
    assert loaded == {"enabled": True}


def test_file_create_json_if_absent_preserves_first_value(tmp_path: pathlib.Path) -> None:
    provider = create_storage_provider(data_dir=tmp_path, backend="file")

    assert provider.create_json_if_absent(
        SCOPE_STATE,
        "maintenance/checkpoint.json",
        {"cursor": "first"},
    )
    assert not provider.create_json_if_absent(
        SCOPE_STATE,
        "maintenance/checkpoint.json",
        {"cursor": "second"},
    )
    assert provider.get_json(SCOPE_STATE, "maintenance/checkpoint.json") == {"cursor": "first"}
    assert not provider.delete_json_if_matches(
        SCOPE_STATE,
        "maintenance/checkpoint.json",
        {"cursor": "second"},
    )
    assert provider.replace_json_if_matches(
        SCOPE_STATE,
        "maintenance/checkpoint.json",
        {"cursor": "first"},
        {"cursor": "replacement"},
    )
    assert provider.delete_json_if_matches(
        SCOPE_STATE,
        "maintenance/checkpoint.json",
        {"cursor": "replacement"},
    )
    assert provider.get_json(SCOPE_STATE, "maintenance/checkpoint.json") is None


@pytest.mark.parametrize("operation", ["create", "replace"])
def test_file_provider_conditional_json_writes_fsync_parent_directory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    provider = create_storage_provider(data_dir=tmp_path, backend="file")
    key = "skills/tasks-scheduler/schedules/one.json"
    initial = {"revision": 1}
    if operation == "replace":
        assert provider.create_json_if_absent(SCOPE_STATE, key, initial)

    original_fsync = file_backend.os.fsync
    fsynced_modes: list[int] = []

    def record_fsync(descriptor: int) -> None:
        fsynced_modes.append(file_backend.os.fstat(descriptor).st_mode)
        original_fsync(descriptor)

    monkeypatch.setattr(file_backend.os, "fsync", record_fsync)

    if operation == "create":
        assert provider.create_json_if_absent(SCOPE_STATE, key, initial)
    else:
        assert provider.replace_json_if_matches(SCOPE_STATE, key, initial, {"revision": 2})

    assert any(stat.S_ISDIR(mode) for mode in fsynced_modes)


def test_file_create_json_if_absent_removes_partial_write(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = create_storage_provider(data_dir=tmp_path, backend="file")
    original_write = os.write

    def fail_write(*_args) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", fail_write)

    with pytest.raises(OSError, match="disk full"):
        provider.create_json_if_absent(
            SCOPE_STATE,
            "maintenance/checkpoint.json",
            {"cursor": "first"},
        )

    monkeypatch.setattr(os, "write", original_write)
    assert provider.create_json_if_absent(
        SCOPE_STATE,
        "maintenance/checkpoint.json",
        {"cursor": "retry"},
    )


def test_file_create_json_if_absent_publishes_complete_document(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = create_storage_provider(data_dir=tmp_path, backend="file")
    write_started = threading.Event()
    finish_write = threading.Event()
    original_write = os.write

    def blocking_write(fd: int, data: bytes) -> int:
        write_started.set()
        assert finish_write.wait(2)
        return original_write(fd, data)

    monkeypatch.setattr(os, "write", blocking_write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            provider.create_json_if_absent,
            SCOPE_STATE,
            "maintenance/checkpoint.json",
            {"cursor": "complete"},
        )
        assert write_started.wait(1)
        try:
            assert not provider.exists(
                SCOPE_STATE,
                "maintenance/checkpoint.json",
            )
        finally:
            finish_write.set()
        assert future.result(timeout=2)

    assert provider.get_json(SCOPE_STATE, "maintenance/checkpoint.json") == {"cursor": "complete"}


def test_file_create_json_if_absent_retries_short_write(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = create_storage_provider(data_dir=tmp_path, backend="file")
    original_write = os.write
    first_write = True

    def short_write(fd: int, data: bytes) -> int:
        nonlocal first_write
        if first_write:
            first_write = False
            return original_write(fd, data[:4])
        return original_write(fd, data)

    monkeypatch.setattr(os, "write", short_write)

    assert provider.create_json_if_absent(
        SCOPE_STATE,
        "maintenance/checkpoint.json",
        {"cursor": "complete"},
    )
    assert provider.get_json(SCOPE_STATE, "maintenance/checkpoint.json") == {"cursor": "complete"}


def test_file_provider_cas_serializes_atomic_json_writer(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = create_storage_provider(data_dir=tmp_path, backend="file")
    key = "skills/tasks-scheduler/schedules/one.json"
    initial = {"revision": 1}
    cas_value = {"revision": 2, "writer": "cas"}
    later_value = {"revision": 3, "writer": "atomic_put"}
    assert provider.create_json_if_absent(SCOPE_STATE, key, initial)

    comparison_started = threading.Event()
    allow_cas_replace = threading.Event()
    atomic_put_started = threading.Event()
    atomic_put_finished = threading.Event()
    original_loads = file_backend.json.loads

    def pause_cas_after_read(value, *args, **kwargs):
        if threading.current_thread().name == "cas-writer":
            comparison_started.set()
            assert allow_cas_replace.wait(timeout=2)
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(file_backend.json, "loads", pause_cas_after_read)
    cas_result: list[bool] = []

    def replace_from_cas() -> None:
        cas_result.append(provider.replace_json_if_matches(SCOPE_STATE, key, initial, cas_value))

    def write_after_cas_started() -> None:
        atomic_put_started.set()
        provider.atomic_put_json(SCOPE_STATE, key, later_value)
        atomic_put_finished.set()

    cas_thread = threading.Thread(target=replace_from_cas, name="cas-writer")
    atomic_thread = threading.Thread(target=write_after_cas_started, name="atomic-writer")
    cas_thread.start()
    assert comparison_started.wait(timeout=2)
    try:
        atomic_thread.start()
        assert atomic_put_started.wait(timeout=2)
        assert not atomic_put_finished.wait(timeout=0.2)
    finally:
        allow_cas_replace.set()
        cas_thread.join(timeout=2)
        atomic_thread.join(timeout=2)

    assert cas_result == [True]
    assert not cas_thread.is_alive()
    assert not atomic_thread.is_alive()
    assert provider.get_json(SCOPE_STATE, key) == later_value


def test_file_provider_streams_file_objects(tmp_path: pathlib.Path) -> None:
    class ChunkedOnlyStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert size >= 0, "stream backend must not read the whole upload at once"
            return super().read(size)

    provider = create_storage_provider(data_dir=tmp_path, backend="file")
    payload = b"upload-data" * 100_000

    provider.put_fileobj(
        "uploads",
        "chat/test/upload.bin",
        ChunkedOnlyStream(payload),
        content_type="application/octet-stream",
    )

    assert provider.get_bytes("uploads", "chat/test/upload.bin") == payload


def test_file_provider_bounded_copy_removes_partial_file(tmp_path: pathlib.Path) -> None:
    from ouroboros.storage.blob.protocol import ObjectTooLargeError

    provider = create_storage_provider(data_dir=tmp_path / "data", backend="file")
    provider.put_bytes("uploads", "chat/test/upload.bin", b"12345")
    destination = tmp_path / "copy.bin"

    with pytest.raises(ObjectTooLargeError, match="exceeds"):
        provider.copy_to_path(
            "uploads",
            "chat/test/upload.bin",
            destination,
            max_bytes=4,
        )

    assert not destination.exists()


def test_file_append_event_logs(tmp_path: pathlib.Path) -> None:
    provider = create_storage_provider(data_dir=tmp_path, backend="file")
    assert provider.append_event("logs", "custom", {"type": "ping", "n": 1})
    assert provider.append_event("logs", "custom", {"type": "pong", "n": 2})
    keys = provider.list_keys("logs", "")
    assert "custom.jsonl" in keys
    raw = provider.get_text("logs", "custom.jsonl")
    assert raw is not None
    lines = [line for line in raw.strip().splitlines() if line]
    assert len(lines) == 2


def test_file_provider_rejects_runtime_events_jsonl(tmp_path: pathlib.Path) -> None:
    provider = create_storage_provider(data_dir=tmp_path, backend="file")
    for stream in ("events", "tools", "supervisor"):
        assert provider.append_event("logs", stream, {"type": "ping"}) is False
        assert not (tmp_path / "logs" / f"{stream}.jsonl").exists()


def test_file_listing_honors_limit_and_cursor(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = create_storage_provider(data_dir=tmp_path, backend="file")
    for index in range(3):
        provider.put_text(SCOPE_MEMORY, f"wide/{index}/item.txt", "content")
    (tmp_path / SCOPE_MEMORY / "wide/link").symlink_to(
        tmp_path / SCOPE_MEMORY / "wide/2",
        target_is_directory=True,
    )

    original_iterdir = pathlib.Path.iterdir
    visited: list[pathlib.Path] = []

    def _iterdir(path: pathlib.Path):
        visited.append(path)
        return original_iterdir(path)

    monkeypatch.setattr(pathlib.Path, "iterdir", _iterdir)
    monkeypatch.setattr(
        pathlib.Path,
        "rglob",
        lambda *_args, **_kwargs: pytest.fail("bounded listing traversed the complete tree"),
    )

    assert provider.list_keys(SCOPE_MEMORY, "wide/", limit=2) == [
        "wide/0/item.txt",
        "wide/1/item.txt",
    ]
    assert tmp_path / SCOPE_MEMORY / "wide" / "2" not in visited
    assert provider.list_keys(
        SCOPE_MEMORY,
        "wide/",
        limit=2,
        start_after="wide/1/item.txt",
    ) == ["wide/2/item.txt"]
    assert provider.list_keys(SCOPE_MEMORY, "wide/", limit=0) == []


def test_drive_storage_maps_data_layout(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    reset_storage_provider_for_tests()
    from ouroboros.storage.blob.drive import get_drive_storage

    (tmp_path / "memory").mkdir(parents=True)
    drive = get_drive_storage(tmp_path)
    path = tmp_path / "memory" / "identity.md"
    drive.write_text(path, "hello")
    assert drive.read_text(path) == "hello"
