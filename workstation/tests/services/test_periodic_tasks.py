"""Maintenance and cleanup service tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import SessionStatus
from app.schemas.session import SessionCreateRequest
from app.services.exec_locks import session_exec_operations
from app.services.session.factory import build_session_service
from app.services.session.service import SessionCreateResult


async def _create_session(service, session_id: str) -> SessionCreateResult:  # type: ignore[no-untyped-def]
    return await service.create_session(session_id=session_id, payload=SessionCreateRequest())


@pytest.mark.asyncio
async def test_cleanup_expired_sessions_terminates_rootfs(session: AsyncSession) -> None:
    service = build_session_service(session)
    result = await _create_session(service, "cleanup-expired")
    result.session.expires_at = result.session.created_at
    await session.flush()

    cleaned = await service.cleanup_expired_sessions()

    assert cleaned == 1
    assert result.session.status == SessionStatus.TERMINATED.value
    assert not Path(result.session.rootfs_path).exists()


@pytest.mark.asyncio
async def test_cleanup_expired_sessions_skips_active_exec(session: AsyncSession) -> None:
    service = build_session_service(session)
    result = await _create_session(service, "cleanup-active-exec")
    result.session.expires_at = result.session.created_at
    await session.flush()

    async with session_exec_operations.track(result.session.id):
        cleaned = await service.cleanup_expired_sessions()

    assert cleaned == 0
    assert result.session.status == SessionStatus.READY.value
    assert Path(result.session.rootfs_path).exists()


@pytest.mark.asyncio
async def test_reconcile_marks_missing_rootfs_failed_and_removes_orphans(session: AsyncSession) -> None:
    service = build_session_service(session)
    result = await _create_session(service, "reconcile-missing")
    orphan_rootfs = Path(result.session.rootfs_path).parent / "orphan-rootfs"
    orphan_rootfs.mkdir(parents=True, exist_ok=True)
    moved_rootfs = Path(result.session.rootfs_path).with_name("moved-rootfs")
    Path(result.session.rootfs_path).rename(moved_rootfs)

    reconcile = await service.reconcile_sessions()

    assert reconcile.failed_missing_rootfs == 1
    assert reconcile.removed_orphans == 2
    assert result.session.status == SessionStatus.FAILED.value
    assert not orphan_rootfs.exists()
    assert not moved_rootfs.exists()


@pytest.mark.asyncio
async def test_reconcile_refreshes_file_usage_from_disk(session: AsyncSession) -> None:
    service = build_session_service(session)
    result = await _create_session(service, "reconcile-usage")
    payload = Path(result.session.rootfs_path) / "workspace" / "data.bin"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(b"1234567890")
    result.session.file_bytes_used = 0
    await session.flush()

    reconcile = await service.reconcile_sessions()
    expected_bytes = sum(
        (Path(root) / filename).stat().st_size
        for root, _, filenames in os.walk(result.session.rootfs_path)
        for filename in filenames
    )

    assert reconcile.failed_missing_rootfs == 0
    assert result.session.file_bytes_used == expected_bytes
