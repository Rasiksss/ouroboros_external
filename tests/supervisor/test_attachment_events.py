from __future__ import annotations

import base64
import logging
import pathlib
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

from sqlalchemy import text

from ouroboros.agent_events.interfaces import FileSendRequested, PhotoSendRequested
from ouroboros.contracts.attachments import StoredFile
from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.chat_attachments import cleanup_expired_attachments
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.events import dispatch_event
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.files import interrupted_upload, outbound_bytes
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, FakeCancellation, scope_with


@parametrize_from_dict(
    {name: {"outcome": name} for name in ("success", "delivery_failure", "unknown", "too_large", "storage_failure")}
)
def test_send_file_event_stores_reference_and_removes_staging(tmp_path, isolate_settings, caplog, monkeypatch, outcome):

    staged = tmp_path / "data" / "tmp" / "outbound" / "report.txt"
    staged.parent.mkdir(parents=True)
    staged.write_text("result", encoding="utf-8")
    read_bytes = pathlib.Path.read_bytes

    def read_other_path(path):
        if path == staged:
            raise AssertionError("staged file must be streamed to storage")
        return read_bytes(path)

    monkeypatch.setattr(pathlib.Path, "read_bytes", read_other_path)
    calls = []
    ctx = SimpleNamespace(
        state_repo=SimpleNamespace(audit_event_repository=sql_audit_event_repository()),
        DRIVE_ROOT=tmp_path / "data",
        bridge=SimpleNamespace(
            send_file=lambda *args, **kwargs: (
                calls.append((args, kwargs))
                or (
                    (True, "ok")
                    if outcome == "success"
                    else (False, DELIVERY_UNKNOWN if outcome == "unknown" else "temporary failure")
                )
            )
        ),
    )

    if outcome == "too_large":
        isolate_settings.save_settings({"OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES": 5})

    with interrupted_upload() if outcome == "storage_failure" else nullcontext():
        dispatch_event(
            FileSendRequested(
                scope=TEST_SCOPE,
                file_path=str(staged),
                filename="report.txt",
                mime="text/plain",
                cleanup_after_send=True,
                source="browser",
            ),
            ctx,
        )

    assert len(calls) == (0 if outcome in {"too_large", "storage_failure"} else 1)
    if calls:
        (media,), kwargs = calls[0]
        assert isinstance(media, StoredFile)
        assert kwargs["scope"] == TEST_SCOPE
        if outcome in {"success", "unknown"}:
            assert outbound_bytes(media, TEST_SCOPE) == b"result"
    if outcome == "too_large":
        assert "reason=attachment_too_large" in caplog.text
    assert not staged.exists()
    provider = get_storage_provider()
    if outcome == "storage_failure":
        with runtime_persistence.current.engine.begin() as conn:
            assert conn.execute(text("SELECT status FROM files")).scalars().all() == ["deleting"]
            conn.execute(text("UPDATE files SET claim_until='2000-01-01'"))
    cleanup_expired_attachments(provider, cancellation=FakeCancellation())
    contents = [
        provider.get_bytes(TEST_SCOPE, SCOPE_UPLOADS, key)
        for key in provider.list_keys(TEST_SCOPE, SCOPE_UPLOADS, "chat/")
    ]
    assert [content for content in contents if content] == ([b"result"] if outcome in {"success", "unknown"} else [])


def test_send_file_event_rejects_arbitrary_tmp_file(tmp_path):

    arbitrary = tmp_path / "data" / "tmp" / "other-task" / "report.txt"
    arbitrary.parent.mkdir(parents=True)
    arbitrary.write_text("result", encoding="utf-8")
    calls = []
    ctx = SimpleNamespace(
        state_repo=SimpleNamespace(audit_event_repository=sql_audit_event_repository()),
        DRIVE_ROOT=tmp_path / "data",
        bridge=SimpleNamespace(send_file=lambda *args, **kwargs: calls.append((args, kwargs)) or (True, "ok")),
    )

    dispatch_event(
        FileSendRequested(
            scope=scope_with(TEST_SCOPE, chat_id="42"),
            file_path=str(arbitrary),
            filename="report.txt",
            mime="text/plain",
        ),
        ctx,
    )

    assert calls == []


def test_send_file_event_logs_success_and_failure_safely(tmp_path, caplog):
    drive_root = tmp_path / "data"
    store = runtime_persistence.initialize()
    staged = drive_root / "tmp" / "outbound" / "job-private" / "report.txt"
    staged.parent.mkdir(parents=True)
    staged.write_text("private report body", encoding="utf-8")
    arbitrary = drive_root / "tmp" / "private-task-id" / "report.txt"
    arbitrary.parent.mkdir(parents=True)
    arbitrary.write_text("private report body", encoding="utf-8")
    ctx = SimpleNamespace(
        state_repo=SimpleNamespace(audit_event_repository=sql_audit_event_repository()),
        DRIVE_ROOT=drive_root,
        bridge=SimpleNamespace(send_file=lambda *_args, **_kwargs: (True, "sdk-mid-42")),
    )

    with caplog.at_level(logging.INFO, logger="supervisor.events"):
        event = FileSendRequested(
            scope=scope_with(TEST_SCOPE, surface_id="dialogs", chat_id="peer-42"),
            task_id="task-1",
            filename="report.txt",
            mime="text/plain",
            source="dialogs",
            transport={"source_message_id": "raw-transport-secret", "target_uid": "private-target"},
        )
        dispatch_event(replace(event, file_path=str(staged), cleanup_after_send=True), ctx)
        dispatch_event(replace(event, file_path=str(arbitrary)), ctx)

    operational = "\n".join(record.getMessage() for record in caplog.records)
    assert operational.count("event=send_file_event_started") == 2
    assert operational.count("event=send_file_transport_selected") == 1
    # Queued, not succeeded: the reading transport sends the bytes later.
    assert operational.count("event=send_file_event_queued") == 1
    assert operational.count("event=send_file_event_error") == 1
    for field in ("task_id=task-1", "filename=report.txt", "transport=dialogs", "response_id=sdk-mid-42"):
        assert field in operational
    assert "stage=event_validation" in operational
    assert "reason=attachment_path_outside_outbound_staging" in operational
    for secret in ("private report body", "raw-transport-secret", "private-target", "job-private", "private-task-id"):
        assert secret not in operational
    audit = (
        sql_audit_event_repository(store.engine)
        .list_audit_events(event_type="send_file_event_error", log_stream="supervisor", limit=1)[0]
        .metadata
    )
    assert audit["task_id"] == "task-1"
    assert audit["stage"] == "event_validation"


def test_legacy_send_photo_rejects_mime_signature_mismatch(tmp_path):
    calls = []
    ctx = SimpleNamespace(
        state_repo=SimpleNamespace(audit_event_repository=sql_audit_event_repository()),
        DRIVE_ROOT=tmp_path / "data",
        bridge=SimpleNamespace(send_image=lambda *args, **kwargs: calls.append((args, kwargs)) or (True, "ok")),
    )

    dispatch_event(
        PhotoSendRequested(
            scope=scope_with(TEST_SCOPE, chat_id="42"),
            image_base64=base64.b64encode(b"not-a-png").decode("ascii"),
            mime="image/png",
        ),
        ctx,
    )

    assert calls == []
