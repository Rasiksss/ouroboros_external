from __future__ import annotations

import logging
import pathlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from new_core.tools.context import ToolContext
from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN
from ouroboros.storage.blob.factory import create_storage_provider
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.chat_attachments import store_attachment_bytes
from ouroboros.tools.service.messaging import get_tools


def _tool(name: str):
    return next(entry.handler for entry in get_tools() if entry.name == name)


def _ooxml_stub_bytes() -> bytes:
    """Satisfy the container signature; parser behavior is isolated in this MIME test."""
    return b"PK\x03\x04xlsx-content-is-not-under-test"


def _ctx(
    tmp_path,
    *,
    source: str = "",
    transport=None,
    task_id: str = "task-1",
    chat_id: int | str = 0,
    drive_root: pathlib.Path | None = None,
    media_senders=None,
) -> ToolContext:
    drive_root = drive_root or tmp_path / "data"
    workspace = drive_root / "tmp" / "task-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        repo_dir=tmp_path,
        drive_root=drive_root,
        current_chat_id=chat_id,
        current_task_source=source,
        transport=dict(transport or {}),
        task_id=task_id,
        task_workspace=workspace,
        media_senders=media_senders
        or {"file": Mock(return_value=(True, "ok")), "image": Mock(return_value=(True, "ok"))},
    )


@pytest.mark.parametrize(
    ("transport_result", "expected"),
    [
        ((True, "mid-1"), "OK:"),
        ((False, "offline"), "⚠️ SEND_FILE_DELIVERY_ERROR:"),
        ((False, DELIVERY_UNKNOWN), "⚠️ SEND_FILE_DELIVERY_UNKNOWN:"),
        (False, "⚠️ SEND_FILE_DELIVERY_ERROR:"),
        (None, "⚠️ SEND_FILE_DELIVERY_ERROR:"),
        ((), "⚠️ SEND_FILE_DELIVERY_ERROR:"),
    ],
)
def test_send_file_calls_transport_synchronously(tmp_path, transport_result, expected):
    sender = Mock(return_value=transport_result)
    ctx = _ctx(tmp_path, source="dialogs", media_senders={"file": sender})
    workspace = ctx.task_workspace
    assert workspace is not None
    report = workspace / "report.txt"
    report.write_text("report", encoding="utf-8")

    result = _tool("send_file")(ctx, file_path=str(report), caption="report")

    assert result.startswith(expected)
    assert ctx.pending_events == []
    assert sender.call_args.args == (0, b"report")
    assert sender.call_args.kwargs["source"] == "dialogs"


def test_send_file_sends_data_write_artifact_owned_by_current_task(tmp_path, caplog):
    from ouroboros.storage.settings.paths import load_path_settings
    from ouroboros.tools.core import _data_write

    drive_root = load_path_settings().data_dir
    ctx = _ctx(
        tmp_path,
        source="dialogs",
        task_id="task-current",
        chat_id="peer-42",
        drive_root=drive_root,
        transport={"source_message_id": "secret-mid"},
    )
    with caplog.at_level(logging.INFO):
        assert _data_write(ctx, "uploads/report.txt", "private ", mode="append").startswith("OK")
        assert caplog.text.count("event=user_artifact_created") == 1
        assert _data_write(ctx, "uploads/report.txt", "draft", mode="append").startswith("OK")
        assert _data_write(ctx, "uploads/report.txt", "private report").startswith("OK")
        assert caplog.text.count("event=user_artifact_created") == 1
        result = _tool("send_file")(ctx, file_path="uploads/report.txt", caption="report")

    assert result.startswith("OK:")
    args, kwargs = ctx.media_senders["file"].call_args
    assert args == ("peer-42", b"private report")
    assert kwargs["task_id"] == "task-current"
    assert kwargs["transport"]["source_message_id"] == "secret-mid"
    assert "event=user_artifact_created" in caplog.text
    assert "event=send_file_succeeded" in caplog.text
    assert "private report" not in caplog.text
    assert "secret-mid" not in caplog.text
    sender = _ctx(tmp_path, task_id="task-sender", drive_root=drive_root)

    result = _tool("send_file")(sender, file_path="uploads/report.txt")

    assert "current task" in result.lower()
    assert sender.pending_events == []


def test_data_write_exists_probe_does_not_block_write(tmp_path, monkeypatch, caplog):
    from ouroboros.storage.settings.paths import load_path_settings
    from ouroboros.tools import core
    from ouroboros.utils import read_text

    drive_root = load_path_settings().data_dir
    ctx = _ctx(tmp_path, task_id="task-current", drive_root=drive_root)
    monkeypatch.setattr(
        core,
        "drive_aware_exists",
        lambda _path: (_ for _ in ()).throw(OSError("storage HEAD failed")),
    )

    with caplog.at_level(logging.INFO):
        result = core._data_write(ctx, "uploads/probe.txt", "report")

    assert result.startswith("OK")
    assert read_text(drive_root / "uploads" / "probe.txt") == "report"
    assert "event=user_artifact_created" not in caplog.text


def test_send_file_materializes_storage_object_without_local_shadow(tmp_path, monkeypatch):
    from ouroboros.storage.blob import drive as drive_module
    from ouroboros.storage.blob.drive import DriveStorage
    from ouroboros.storage.blob.file_backend import FileStorageProvider

    ctx = _ctx(tmp_path, task_id="task-remote")
    provider = FileStorageProvider(tmp_path / "remote")
    provider.put_text(SCOPE_UPLOADS, "report.txt", "remote report")
    provider.put_json(
        SCOPE_UPLOADS,
        "report.txt.metadata.json",
        {
            "origin": "agent_generated",
            "purpose": "artifact",
            "created_by_tool": "data_write",
            "task_id": "task-remote",
        },
    )
    monkeypatch.setattr(
        drive_module,
        "get_drive_storage",
        lambda _root: DriveStorage(ctx.drive_root, provider),
    )

    result = _tool("send_file")(ctx, file_path=str(ctx.drive_root / "uploads" / "report.txt"))

    assert result.startswith("OK:")
    assert ctx.media_senders["file"].call_args.args[1] == b"remote report"


def test_send_file_sends_existing_stored_attachment_by_id(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, source="browser", task_id="task-resend")
    from ouroboros.storage.chat_store import append_chat_message

    message = append_chat_message(
        ctx.drive_root,
        actor_type="owner",
        chat_id=0,
        user_id=0,
        text="photo",
        source="browser",
        client_message_id="browser-message-1",
    )
    assert message is not None
    metadata = store_attachment_bytes(
        create_storage_provider(data_dir=ctx.drive_root),
        attachment_id="attachment-resend",
        filename="photo.png",
        mime="image/png",
        data=b"\x89PNG\r\n\x1a\nstored image",
        source="browser",
        message_id="browser-message-1",
    )
    monkeypatch.chdir(ctx.task_workspace)
    assert _tool("send_file")(ctx, file_path=metadata["id"]).startswith("OK:")

    historical_path = f"uploads/{metadata['storage_key']}"
    assert _tool("send_image")(ctx, file_path=historical_path).startswith("OK:")
    assert _tool("send_image")(ctx, file_path="attachment-missing").startswith("⚠️ SEND_IMAGE_NOT_FOUND:")
    assert ctx.media_senders["file"].call_args.args[1] == b"\x89PNG\r\n\x1a\nstored image"
    assert ctx.media_senders["image"].call_args.kwargs["filename"] == "photo.png"
    other_chat = _ctx(tmp_path, source="dialogs", chat_id=1, drive_root=ctx.drive_root)
    assert _tool("send_file")(other_chat, file_path=metadata["id"]).startswith("OK:")
    assert other_chat.media_senders["file"].call_args.args[1] == b"\x89PNG\r\n\x1a\nstored image"


def _blind_mimetypes(monkeypatch):
    from ouroboros.tools.service import messaging

    monkeypatch.setattr(messaging.mimetypes, "guess_type", lambda *_args, **_kwargs: (None, None))


def test_send_file_resends_stored_office_attachment_without_mimetypes_database(tmp_path, monkeypatch):
    from ouroboros.tools import attachments

    _blind_mimetypes(monkeypatch)
    monkeypatch.setattr(attachments, "run_parser", lambda *_args, **_kwargs: "")
    ctx = _ctx(tmp_path, source="dialogs", task_id="task-resend-xlsx")
    metadata = store_attachment_bytes(
        create_storage_provider(data_dir=ctx.drive_root),
        attachment_id="attachment-workbook",
        filename="book.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        data=_ooxml_stub_bytes(),
        source="dialogs",
    )

    result = _tool("send_file")(ctx, file_path=metadata["id"])

    assert result.startswith("OK:")
    assert ctx.media_senders["file"].call_args.kwargs["mime"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_send_file_sends_workspace_markdown_without_mimetypes_database(tmp_path, monkeypatch):
    _blind_mimetypes(monkeypatch)
    ctx = _ctx(tmp_path, source="dialogs")
    workspace = ctx.task_workspace
    assert workspace is not None
    report = workspace / "report.md"
    report.write_text("# report", encoding="utf-8")

    result = _tool("send_file")(ctx, file_path=str(report))

    assert result.startswith("OK:")
    assert ctx.media_senders["file"].call_args.kwargs["mime"] == "text/markdown"


def test_send_file_rejects_stored_attachment_with_contradicting_mime(tmp_path):
    ctx = _ctx(tmp_path, source="dialogs")
    metadata = store_attachment_bytes(
        create_storage_provider(data_dir=ctx.drive_root),
        attachment_id="attachment-mismatch",
        filename="report.pdf",
        mime="text/plain",
        data=b"%PDF-1.4\nnot really\n",
        source="dialogs",
    )

    result = _tool("send_file")(ctx, file_path=metadata["id"])

    assert result.startswith("⚠️ SEND_FILE_")
    assert not ctx.media_senders["file"].called


def test_send_file_storage_failure_keeps_safe_audit_cause(tmp_path, monkeypatch):
    from ouroboros.storage.blob import drive as drive_module
    from ouroboros.storage.runtime_store import init_runtime_store

    ctx = _ctx(tmp_path, task_id="task-storage")
    store = init_runtime_store(ctx.drive_root)
    provider = SimpleNamespace(
        exists=lambda *_args: (_ for _ in ()).throw(PermissionError("storage-token\nprivate path"))
    )
    monkeypatch.setattr(
        drive_module,
        "get_drive_storage",
        lambda _root: SimpleNamespace(provider=provider),
    )

    result = _tool("send_file")(ctx, file_path="uploads/report.txt")

    assert result.startswith("⚠️ SEND_FILE_STORAGE_ERROR:")
    audit = store.list_audit_events(event_type="send_file_materialization_error", log_stream="tools", limit=1)[
        0
    ].metadata
    assert (audit["task_id"], audit["stage"], audit["exception_type"]) == (
        "task-storage",
        "storage_materialization",
        "PermissionError",
    )
    assert "storage-token" not in str(audit)


def test_send_user_message_preserves_origin_transport(tmp_path):
    ctx = _ctx(
        tmp_path,
        source="dialogs",
        transport={"source_message_id": "mid-1", "target_uid": "alice"},
    )

    result = _tool("send_user_message")(ctx, text="working")

    assert result.startswith("OK")
    assert ctx.pending_events[0]["transport"] == {
        "source_message_id": "mid-1",
        "target_uid": "alice",
    }


@pytest.mark.parametrize(
    ("location", "filename", "expected"),
    [
        ("uploads", "report.txt", "task workspace"),
        ("other-task", "report.txt", "task workspace"),
        ("workspace", "result.zip", "unsupported file format"),
    ],
)
def test_send_file_rejects_unowned_or_forbidden_path(tmp_path, location, filename, expected):
    ctx = _ctx(tmp_path)
    base = {
        "uploads": tmp_path / "data" / "uploads",
        "other-task": tmp_path / "data" / "tmp" / "other-task",
        "workspace": ctx.task_workspace,
    }[location]
    assert base is not None
    base.mkdir(parents=True, exist_ok=True)
    path = base / filename
    path.write_bytes(b"PK\x03\x04data" if filename.endswith(".zip") else b"report")

    result = _tool("send_file")(ctx, file_path=str(path))

    assert expected in result.lower()
    assert ctx.pending_events == []


@pytest.mark.parametrize("delivered", [True, False])
def test_send_file_event_always_removes_staged_artifact(tmp_path, delivered):
    from supervisor.events import _handle_send_file

    staged = tmp_path / "data" / "tmp" / "outbound" / "report.txt"
    staged.parent.mkdir(parents=True)
    staged.write_text("result", encoding="utf-8")
    calls = []
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path / "data",
        send_file=lambda *args, **kwargs: (
            calls.append((args, kwargs)) or ((True, "ok") if delivered else (False, "temporary failure"))
        ),
        bridge=SimpleNamespace(send_file=lambda *_a, **_k: (True, "ok")),
    )

    _handle_send_file(
        {
            "chat_id": 0,
            "file_path": str(staged),
            "filename": "report.txt",
            "mime": "text/plain",
            "cleanup_after_send": True,
        },
        ctx,
    )

    assert calls
    assert not staged.exists()


def test_send_file_event_rejects_arbitrary_tmp_file(tmp_path):
    from supervisor.events import _handle_send_file

    arbitrary = tmp_path / "data" / "tmp" / "other-task" / "report.txt"
    arbitrary.parent.mkdir(parents=True)
    arbitrary.write_text("result", encoding="utf-8")
    calls = []
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path / "data",
        send_file=lambda *args, **kwargs: calls.append((args, kwargs)) or (True, "ok"),
        bridge=SimpleNamespace(send_file=lambda *_a, **_k: (True, "ok")),
    )

    _handle_send_file(
        {
            "chat_id": 42,
            "file_path": str(arbitrary),
            "filename": "report.txt",
            "mime": "text/plain",
        },
        ctx,
    )

    assert calls == []


def test_send_file_event_logs_success_and_failure_safely(tmp_path, caplog):
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.events import _handle_send_file

    drive_root = tmp_path / "data"
    store = init_runtime_store(drive_root)
    staged = drive_root / "tmp" / "outbound" / "job-private" / "report.txt"
    staged.parent.mkdir(parents=True)
    staged.write_text("private report body", encoding="utf-8")
    arbitrary = drive_root / "tmp" / "private-task-id" / "report.txt"
    arbitrary.parent.mkdir(parents=True)
    arbitrary.write_text("private report body", encoding="utf-8")
    ctx = SimpleNamespace(
        DRIVE_ROOT=drive_root,
        send_file=lambda *_args, **_kwargs: (True, "sdk-mid-42"),
        bridge=SimpleNamespace(send_file=lambda *_a, **_k: (True, "browser-mid")),
    )

    with caplog.at_level(logging.INFO, logger="supervisor.events"):
        event = {
            "task_id": "task-1",
            "chat_id": "peer-42",
            "filename": "report.txt",
            "mime": "text/plain",
            "source": "dialogs",
            "transport": {"source_message_id": "raw-transport-secret", "target_uid": "private-target"},
        }
        _handle_send_file({**event, "file_path": str(staged), "cleanup_after_send": True}, ctx)
        _handle_send_file({**event, "file_path": str(arbitrary)}, ctx)

    operational = "\n".join(record.getMessage() for record in caplog.records)
    assert operational.count("event=send_file_event_started") == 2
    assert operational.count("event=send_file_transport_selected") == 1
    assert operational.count("event=send_file_event_succeeded") == 1
    assert operational.count("event=send_file_event_error") == 1
    for field in ("task_id=task-1", "filename=report.txt", "transport=dialogs", "response_id=sdk-mid-42"):
        assert field in operational
    assert "stage=event_validation" in operational
    assert "reason=attachment_path_outside_outbound_staging" in operational
    for secret in ("private report body", "raw-transport-secret", "private-target", "job-private", "private-task-id"):
        assert secret not in operational
    audit = store.list_audit_events(event_type="send_file_event_error", log_stream="supervisor", limit=1)[0].metadata
    assert audit["task_id"] == "task-1"
    assert audit["stage"] == "event_validation"


def test_legacy_send_photo_rejects_mime_signature_mismatch(tmp_path):
    import base64

    from supervisor.events import _handle_send_photo

    calls = []
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path / "data",
        send_image=lambda *args, **kwargs: calls.append((args, kwargs)) or (True, "ok"),
        bridge=SimpleNamespace(send_photo=lambda *_a, **_k: (True, "ok")),
    )

    _handle_send_photo(
        {
            "chat_id": 42,
            "image_base64": base64.b64encode(b"not-a-png").decode("ascii"),
            "mime": "image/png",
        },
        ctx,
    )

    assert calls == []


@pytest.mark.parametrize("insert_conflict", [False, True])
def test_send_photo_does_not_broadcast_when_history_persistence_fails(
    monkeypatch,
    tmp_path,
    insert_conflict,
):
    from supervisor import message_bus

    data_dir = tmp_path / "data"
    monkeypatch.setattr(message_bus, "_try_drive_root", lambda: data_dir)
    monkeypatch.setattr(
        message_bus,
        "load_state",
        lambda: SimpleNamespace(owner_id=1, session_id="session-1"),
    )
    append = (
        (lambda *_args, **_kwargs: None)
        if insert_conflict
        else (lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("database unavailable")))
    )
    monkeypatch.setattr(message_bus, "append_chat_message", append)
    bridge = message_bus.LocalChatBridge({})
    broadcasts = []
    bridge.set_browser_sink(broadcasts.append)

    result = bridge.send_photo(
        1,
        b"\x89PNG\r\n\x1a\npayload",
        source="browser",
    )

    assert result == (False, "failed to persist attachment")
    assert broadcasts == []
    assert bridge._outbox.empty()
    provider = create_storage_provider(data_dir=data_dir)
    assert provider.list_keys(SCOPE_UPLOADS, "chat/") == []
    assert provider.list_keys(SCOPE_UPLOADS, "_meta/chat/") == []
