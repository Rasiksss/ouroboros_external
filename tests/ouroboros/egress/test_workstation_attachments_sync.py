from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from new_core.tools.context import ToolContext
from ouroboros.egress.workstation.attachments_sync import sync_turn_attachments
from ouroboros.storage.blob.factory import get_storage_provider
from tests.helpers.blob_double import recording_blob_double
from tests.helpers.files import stored_file
from tests.helpers.shared import TEST_SCOPE


def _stored_attachment(tmp_path: pathlib.Path, *, attachment_id: str, data: bytes, filename: str = "a.txt"):
    drive_root = tmp_path / "data"
    provider = get_storage_provider()
    stored_file(
        scope=TEST_SCOPE,
        provider=provider,
        data=data,
        attachment_id=attachment_id,
        filename=filename,
        mime="text/plain",
        source="browser",
    )
    return drive_root


def _handle(**overrides: Any) -> Any:
    handle = MagicMock()
    handle.config.workspace_root = "/workspace"
    handle.config.access_level = "rw"
    handle.config.attachment_max_bytes = 1024
    for key, value in overrides.items():
        setattr(handle.config, key, value)
    return handle


def test_new_attachment_uploaded_to_inbound(tmp_path: pathlib.Path) -> None:
    drive_root = _stored_attachment(tmp_path, attachment_id="attachment-1", data=b"hello")
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive_root,
        attachments=[{"id": "attachment-1"}],
        scope=TEST_SCOPE,
        skill_surface=None,
    )
    handle = _handle()

    sync_turn_attachments(handle, ctx)

    handle.files.write_file.assert_called_once()
    args, kwargs = handle.files.write_file.call_args
    assert args[0] == "media/inbound/a.txt"
    assert args[1] == b"hello"
    assert kwargs["mime"] == "text/plain"


def test_upload_and_forget_reuploads_on_a_later_call(tmp_path: pathlib.Path) -> None:
    """No ledger: the same attachment id resurfacing in a later turn's
    ``ctx.attachments`` (e.g. the user re-references an older upload) is
    re-synced rather than silently skipped. The overwrite is harmless — same
    bytes, same path — and not worth persistent state to avoid."""
    drive_root = _stored_attachment(tmp_path, attachment_id="attachment-1", data=b"hello")
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive_root,
        attachments=[{"id": "attachment-1"}],
        scope=TEST_SCOPE,
        skill_surface=None,
    )
    handle = _handle()

    sync_turn_attachments(handle, ctx)
    sync_turn_attachments(handle, ctx)

    assert handle.files.write_file.call_count == 2


@pytest.mark.parametrize("limit_source", ["metadata", "actual_bytes", "format"])
def test_oversize_attachment_is_skipped_without_unbounded_read(
    tmp_path: pathlib.Path, isolate_settings, limit_source
) -> None:
    drive_root = _stored_attachment(tmp_path, attachment_id="attachment-1", data=b"x" * 2000)
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive_root,
        attachments=[{"id": "attachment-1"}],
        scope=TEST_SCOPE,
        skill_surface=None,
    )
    handle = _handle(attachment_max_bytes=1024)
    if limit_source != "metadata":
        from sqlalchemy import text

        from ouroboros.storage.runtime_persistence import runtime_persistence

        with runtime_persistence.current.engine.begin() as conn:
            conn.execute(text("UPDATE files SET size=1 WHERE id='attachment-1'"))
    if limit_source == "format":
        handle.config.attachment_max_bytes = 4096
        isolate_settings.save_settings({"OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES": 1024})

    with recording_blob_double() as recorder:
        sync_turn_attachments(handle, ctx)

    assert recorder.unbounded_reads == [], "attachment sync must use bounded reads"
    handle.files.write_file.assert_not_called()


def test_missing_metadata_is_skipped_without_raising(tmp_path: pathlib.Path) -> None:
    drive_root = tmp_path / "data"
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive_root,
        attachments=[{"id": "does-not-exist"}],
        scope=TEST_SCOPE,
        skill_surface=None,
    )
    handle = _handle()

    sync_turn_attachments(handle, ctx)

    handle.files.write_file.assert_not_called()


def test_write_failure_never_propagates(tmp_path: pathlib.Path) -> None:
    drive_root = _stored_attachment(tmp_path, attachment_id="attachment-1", data=b"hello")
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive_root,
        attachments=[{"id": "attachment-1"}],
        scope=TEST_SCOPE,
        skill_surface=None,
    )
    handle = _handle()
    handle.files.write_file.side_effect = RuntimeError("session dropped")

    sync_turn_attachments(handle, ctx)  # must not raise


def test_no_workspace_access_skips_entirely(tmp_path: pathlib.Path) -> None:
    drive_root = _stored_attachment(tmp_path, attachment_id="attachment-1", data=b"hello")
    ctx = ToolContext(
        repo_dir=tmp_path,
        drive_root=drive_root,
        attachments=[{"id": "attachment-1"}],
        scope=TEST_SCOPE,
        skill_surface=None,
    )
    handle = _handle(access_level="none")

    sync_turn_attachments(handle, ctx)

    handle.files.write_file.assert_not_called()


def test_no_pending_attachments_is_a_noop(tmp_path: pathlib.Path) -> None:
    ctx = ToolContext(
        repo_dir=tmp_path, drive_root=tmp_path / "data", attachments=[], scope=TEST_SCOPE, skill_surface=None
    )
    handle = _handle()

    sync_turn_attachments(handle, ctx)

    handle.files.write_file.assert_not_called()
