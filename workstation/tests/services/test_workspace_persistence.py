"""Tests for durable /workspace snapshot storage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import Session
from app.models.workspace_persistence import WorkspaceScope
from app.repositories.workspace_persistence import WorkspacePersistenceRepository
from app.services import workspace_persistence as persistence_module
from app.services.workspace_persistence import (
    DisabledWorkspaceStore,
    WorkspacePersistenceRequest,
    WorkspacePersistenceService,
)
from app.utils.exceptions.common import InternalServerErrorException


class MemoryWorkspaceStore:
    enabled = True

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_blob(self, sha256: str, content: bytes) -> str:
        key = f"blobs/{sha256}"
        self.objects[key] = content
        return key

    async def put_manifest(self, *, scope_id: str, revision_id: str, manifest: dict[str, Any]) -> str:
        key = f"scopes/{scope_id}/revisions/{revision_id}.json"
        self.objects[key] = json.dumps(manifest).encode("utf-8")
        return key

    async def put_latest(self, *, scope_id: str, manifest: dict[str, Any]) -> str:
        key = f"scopes/{scope_id}/latest.json"
        self.objects[key] = json.dumps(manifest).encode("utf-8")
        return key

    async def get_latest(self, scope_id: str) -> dict[str, Any] | None:
        raw = self.objects.get(f"scopes/{scope_id}/latest.json")
        return json.loads(raw.decode("utf-8")) if raw is not None else None

    async def get_json(self, object_key: str) -> dict[str, Any]:
        return json.loads(self.objects[object_key].decode("utf-8"))

    async def get_blob(self, object_key: str) -> bytes:
        return self.objects[object_key]


def test_workspace_store_toggle_disables_s3_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unexpected_s3_store(**_kwargs: Any) -> None:
        raise AssertionError("S3 store should not be constructed when WORKSPACE_STORE_ENABLED=false")

    monkeypatch.setattr(persistence_module.settings, "WORKSPACE_STORE_ENABLED", False)
    monkeypatch.setattr(persistence_module.settings, "WORKSPACE_STORE_BACKEND", "s3")
    monkeypatch.setattr(persistence_module.settings, "WORKSPACE_STORE_S3_BUCKET", "bucket")
    monkeypatch.setattr(persistence_module, "S3WorkspaceStore", _unexpected_s3_store)

    store = persistence_module.build_workspace_object_store()

    assert isinstance(store, DisabledWorkspaceStore)
    assert store.enabled is False


def test_s3_store_requires_bucket_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(persistence_module.settings, "WORKSPACE_STORE_ENABLED", True)
    monkeypatch.setattr(persistence_module.settings, "WORKSPACE_STORE_BACKEND", "s3")
    monkeypatch.setattr(persistence_module.settings, "WORKSPACE_STORE_S3_BUCKET", None)

    with pytest.raises(RuntimeError, match="WORKSPACE_STORE_S3_BUCKET"):
        persistence_module.build_workspace_object_store()


def _session(
    session_id: str,
    rootfs: Path,
    scope_id: str | None = None,
    created_at: datetime | None = None,
) -> Session:
    return Session(
        id=session_id,
        status="ready",
        token_hash="hash",
        rootfs_path=str(rootfs),
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
        profile="default",
        network_enabled=False,
        idle_timeout_sec=900,
        absolute_ttl_sec=3600,
        file_bytes_used=0,
        workspace_persistence_scope_id=scope_id,
        workspace_persistence_root="/workspace" if scope_id else None,
    )


@pytest.mark.asyncio
async def test_snapshot_and_restore_only_workspace_root(session: AsyncSession, tmp_path: Path) -> None:
    store = MemoryWorkspaceStore()
    service = WorkspacePersistenceService(
        repository=WorkspacePersistenceRepository(session),
        object_store=store,
    )
    rootfs = tmp_path / "one"
    (rootfs / "workspace" / ".agents" / "skills").mkdir(parents=True)
    (rootfs / "workspace" / ".agents" / "skills" / "SKILL.md").write_text("skill", encoding="utf-8")
    (rootfs / "workspace" / "media").mkdir()
    (rootfs / "workspace" / "media" / "image.bin").write_bytes(b"\x00\xffimage")
    (rootfs / "agent").mkdir()
    (rootfs / "agent" / "agent.txt").write_text("agent", encoding="utf-8")
    (rootfs / "bin").mkdir()
    (rootfs / "bin" / "tool").write_text("tool", encoding="utf-8")

    result = await service.snapshot(_session("s1", rootfs, "scope-a"))

    assert result.snapshot is True
    assert result.file_count == 2

    restored_rootfs = tmp_path / "two"
    (restored_rootfs / "workspace").mkdir(parents=True)
    (restored_rootfs / "workspace" / "stale.txt").write_text("stale", encoding="utf-8")
    (restored_rootfs / "agent").mkdir()
    (restored_rootfs / "agent" / "agent.txt").write_text("keep-local-agent", encoding="utf-8")
    restored_session = _session("s2", restored_rootfs)

    restored = await service.restore(
        restored_session,
        WorkspacePersistenceRequest(scope_id="scope-a"),
    )

    assert restored.restored is True
    assert not (restored_rootfs / "workspace" / "stale.txt").exists()
    assert (restored_rootfs / "workspace" / ".agents" / "skills" / "SKILL.md").read_text(encoding="utf-8") == "skill"
    assert (restored_rootfs / "workspace" / "media" / "image.bin").read_bytes() == b"\x00\xffimage"
    assert not (restored_rootfs / "bin").exists()
    assert (restored_rootfs / "agent" / "agent.txt").read_text(encoding="utf-8") == "keep-local-agent"


@pytest.mark.asyncio
async def test_disabled_store_is_noop(session: AsyncSession, tmp_path: Path) -> None:
    service = WorkspacePersistenceService(
        repository=WorkspacePersistenceRepository(session),
        object_store=DisabledWorkspaceStore(),
    )
    rootfs = tmp_path / "disabled"
    (rootfs / "workspace").mkdir(parents=True)
    (rootfs / "workspace" / "file.txt").write_text("hello", encoding="utf-8")

    snapshot = await service.snapshot(_session("s1", rootfs, "scope-disabled"))
    restored = await service.restore(
        _session("s2", rootfs),
        WorkspacePersistenceRequest(scope_id="scope-disabled"),
    )

    assert snapshot.enabled is False
    assert snapshot.snapshot is False
    assert restored.enabled is False
    assert restored.restored is False
    assert (rootfs / "workspace" / "file.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_missing_rootfs_does_not_overwrite_latest_revision(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    store = MemoryWorkspaceStore()
    service = WorkspacePersistenceService(
        repository=WorkspacePersistenceRepository(session),
        object_store=store,
    )
    rootfs = tmp_path / "live"
    (rootfs / "workspace").mkdir(parents=True)
    (rootfs / "workspace" / "file.txt").write_text("durable", encoding="utf-8")
    first = await service.snapshot(_session("s1", rootfs, "scope-live"))

    missing = tmp_path / "missing"
    skipped = await service.snapshot(_session("s2", missing, "scope-live"))

    assert skipped.snapshot is False
    latest = await WorkspacePersistenceRepository(session).get_latest_revision("scope-live")
    assert latest is not None
    assert latest.id == first.revision_id


@pytest.mark.asyncio
async def test_workspace_root_symlink_fails_snapshot(session: AsyncSession, tmp_path: Path) -> None:
    store = MemoryWorkspaceStore()
    service = WorkspacePersistenceService(
        repository=WorkspacePersistenceRepository(session),
        object_store=store,
    )
    rootfs = tmp_path / "rootfs"
    outside = tmp_path / "outside"
    outside.mkdir()
    rootfs.mkdir()
    (rootfs / "workspace").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InternalServerErrorException):
        await service.snapshot(_session("s1", rootfs, "scope-symlink"))


def _workspace_with(rootfs: Path, name: str, content: str) -> Path:
    (rootfs / "workspace").mkdir(parents=True)
    (rootfs / "workspace" / name).write_text(content, encoding="utf-8")
    return rootfs


@pytest.mark.asyncio
async def test_restore_survives_a_wiped_database(session: AsyncSession, tmp_path: Path) -> None:
    store = MemoryWorkspaceStore()
    service = WorkspacePersistenceService(
        repository=WorkspacePersistenceRepository(session),
        object_store=store,
    )
    source = _workspace_with(tmp_path / "source", "notes.txt", "durable")
    await service.snapshot(_session("s1", source, "scope-wipe"))

    # The station's DB may be ephemeral; the pointer lives in the object store.
    await session.execute(delete(WorkspaceScope))
    await session.flush()

    target = tmp_path / "target"
    (target / "workspace").mkdir(parents=True)
    restored = await service.restore(
        _session("s2", target),
        WorkspacePersistenceRequest(scope_id="scope-wipe"),
    )

    assert restored.restored is True
    assert restored.file_count == 1
    assert (target / "workspace" / "notes.txt").read_text(encoding="utf-8") == "durable"


@pytest.mark.asyncio
async def test_restore_without_pointer_is_not_a_failure(session: AsyncSession, tmp_path: Path) -> None:
    service = WorkspacePersistenceService(
        repository=WorkspacePersistenceRepository(session),
        object_store=MemoryWorkspaceStore(),
    )
    target = tmp_path / "fresh"
    (target / "workspace").mkdir(parents=True)
    target_session = _session("s1", target)

    restored = await service.restore(target_session, WorkspacePersistenceRequest(scope_id="scope-new"))

    assert restored.enabled is True
    assert restored.restored is False
    assert target_session.workspace_restored_revision_id is None


@pytest.mark.asyncio
async def test_older_session_does_not_replace_newer_snapshot(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    store = MemoryWorkspaceStore()
    service = WorkspacePersistenceService(
        repository=WorkspacePersistenceRepository(session),
        object_store=store,
    )
    # The orphan was created before the gateway restarted; its replacement after.
    orphan = _workspace_with(tmp_path / "orphan", "file.txt", "pre-restart")
    replacement = _workspace_with(tmp_path / "replacement", "file.txt", "post-restart")
    orphan_session = _session("s1", orphan, "scope-race", created_at=datetime(2026, 5, 1, tzinfo=UTC))
    replacement_session = _session(
        "s2",
        replacement,
        "scope-race",
        created_at=datetime(2026, 5, 1, 1, tzinfo=UTC),
    )

    newer = await service.snapshot(replacement_session)
    # The reaper terminates the orphan later, so it snapshots last.
    older = await service.snapshot(orphan_session)

    assert newer.snapshot is True
    assert older.snapshot is True

    target = tmp_path / "next"
    (target / "workspace").mkdir(parents=True)
    restored = await service.restore(_session("s3", target), WorkspacePersistenceRequest(scope_id="scope-race"))

    assert restored.revision_id == newer.revision_id
    assert (target / "workspace" / "file.txt").read_text(encoding="utf-8") == "post-restart"
    scope = await WorkspacePersistenceRepository(session).get_scope("scope-race")
    assert scope is not None
    assert scope.latest_revision_id == newer.revision_id


@pytest.mark.asyncio
async def test_v1_pointer_is_superseded(session: AsyncSession, tmp_path: Path) -> None:
    store = MemoryWorkspaceStore()
    service = WorkspacePersistenceService(
        repository=WorkspacePersistenceRepository(session),
        object_store=store,
    )
    # A v1 manifest, written before ordering existed, carries no ordering position.
    await store.put_latest(
        scope_id="scope-legacy",
        manifest={"schemaVersion": 1, "revisionId": "legacy", "root": "/workspace", "files": []},
    )
    source = _workspace_with(tmp_path / "current", "file.txt", "current")

    result = await service.snapshot(_session("s1", source, "scope-legacy"))

    latest = await store.get_latest("scope-legacy")
    assert latest is not None
    assert latest["revisionId"] == result.revision_id
