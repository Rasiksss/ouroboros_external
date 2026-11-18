"""File gateway tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.session import Session
from app.services.file_gateway import FileGateway
from app.utils.exceptions.common import (
    NotFoundException,
    PolicyDeniedException,
    QuotaExceededException,
)
from app.utils.time import utcnow


def _build_session(rootfs_path: Path) -> Session:
    now = utcnow()
    rootfs_path.mkdir(parents=True, exist_ok=True)
    return Session(
        id="session-files",
        status="ready",
        token_hash="token",
        rootfs_path=str(rootfs_path),
        created_at=now,
        updated_at=None,
        last_used_at=now,
        expires_at=now,
        idle_timeout_sec=900,
        absolute_ttl_sec=3600,
        file_bytes_used=0,
        fail_reason=None,
        terminated_at=None,
    )


@pytest.mark.asyncio
async def test_upload_file_writes_content_and_updates_quota(tmp_path: Path) -> None:
    session = _build_session(tmp_path / "rootfs")
    gateway = FileGateway()

    result = await gateway.upload_file(
        session,
        path="/workspace/data.bin",
        content=b"hello",
    )

    assert result.path == "/workspace/data.bin"
    assert result.bytes_written == 5
    assert result.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert session.file_bytes_used == 5
    assert (tmp_path / "rootfs" / "workspace" / "data.bin").read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_upload_file_overwrite_adjusts_incremental_usage(tmp_path: Path) -> None:
    session = _build_session(tmp_path / "rootfs")
    gateway = FileGateway()

    await gateway.upload_file(session, path="/workspace/data.bin", content=b"hello")
    await gateway.upload_file(session, path="/workspace/data.bin", content=b"abcdefgh")

    assert session.file_bytes_used == 8


@pytest.mark.asyncio
async def test_upload_file_rejects_total_quota_exceeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _build_session(tmp_path / "rootfs")
    session.file_bytes_used = 8
    gateway = FileGateway()
    monkeypatch.setattr("app.services.file_gateway.settings.MAX_TOTAL_BYTES", 10)

    with pytest.raises(QuotaExceededException):
        await gateway.upload_file(session, path="/workspace/data.bin", content=b"123")


@pytest.mark.asyncio
async def test_upload_file_rejects_symlink_escape(tmp_path: Path) -> None:
    session = _build_session(tmp_path / "rootfs")
    gateway = FileGateway()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (tmp_path / "rootfs" / "escape").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(PolicyDeniedException):
        await gateway.upload_file(session, path="/escape/data.bin", content=b"boom")


@pytest.mark.asyncio
async def test_resolve_file_returns_sha_and_size(tmp_path: Path) -> None:
    session = _build_session(tmp_path / "rootfs")
    gateway = FileGateway()
    target = tmp_path / "rootfs" / "workspace" / "data.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"hello")

    resolved = await gateway.resolve_file(session, path="/workspace/data.txt")

    assert resolved.host_path == target
    assert resolved.bytes_count == 5
    assert resolved.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


@pytest.mark.asyncio
async def test_resolve_file_raises_not_found(tmp_path: Path) -> None:
    session = _build_session(tmp_path / "rootfs")

    with pytest.raises(NotFoundException):
        await FileGateway().resolve_file(session, path="/missing.txt")


@pytest.mark.asyncio
async def test_list_files_returns_tree_with_sha_and_honors_depth(tmp_path: Path) -> None:
    session = _build_session(tmp_path / "rootfs")
    gateway = FileGateway()
    workspace = tmp_path / "rootfs" / "workspace"
    (workspace / "sub").mkdir(parents=True, exist_ok=True)
    (workspace / "a.txt").write_bytes(b"A")
    (workspace / "sub" / "b.txt").write_bytes(b"BB")

    tree = await gateway.list_files(
        session,
        path="/workspace",
        recursive=True,
        max_depth=1,
    )

    assert tree.path == "/workspace"
    assert tree.kind == "dir"
    assert [child.name for child in tree.children] == ["sub", "a.txt"]
    subdir = tree.children[0]
    assert subdir.kind == "dir"
    assert subdir.children == []
    file_node = tree.children[1]
    assert file_node.kind == "file"
    assert file_node.bytes_count == 1
    assert file_node.sha256 == "559aead08264d5795d3909718cdd05abd49572e84fe55590eef31a88a08fdffd"


@pytest.mark.asyncio
async def test_list_files_does_not_follow_symlinks(tmp_path: Path) -> None:
    session = _build_session(tmp_path / "rootfs")
    gateway = FileGateway()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    (tmp_path / "rootfs" / "workspace").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rootfs" / "workspace" / "link").symlink_to(outside / "secret.txt")

    tree = await gateway.list_files(session, path="/workspace")

    assert len(tree.children) == 1
    assert tree.children[0].name == "link"
    assert tree.children[0].kind == "file"
    assert tree.children[0].sha256 is None


@pytest.mark.asyncio
async def test_list_files_can_skip_sha_calculation(tmp_path: Path) -> None:
    session = _build_session(tmp_path / "rootfs")
    gateway = FileGateway()
    workspace = tmp_path / "rootfs" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "a.txt").write_bytes(b"A")

    tree = await gateway.list_files(
        session,
        path="/workspace",
        recursive=True,
        max_depth=1,
        include_sha256=False,
    )

    assert len(tree.children) == 1
    assert tree.children[0].bytes_count == 1
    assert tree.children[0].sha256 is None
