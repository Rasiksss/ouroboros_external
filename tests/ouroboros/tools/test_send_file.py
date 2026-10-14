from __future__ import annotations

import logging
import pathlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from new_core.identity import OwnerScope
from new_core.tools.context import ToolContext
from ouroboros.contracts.attachments import StoredFile
from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.tools.service.messaging import get_tools
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.files import link_file, outbound_bytes, stored_file
from tests.helpers.office import DOCX_MIME, PPTX_MIME, XLSX_MIME, write_deck, write_document, write_workbook
from tests.helpers.outbound import RecordingOutbound
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, make_tool_ctx, scope_with


def _tool(name: str):
    return next(entry.handler for entry in get_tools() if entry.name == name)


class _FixedOutcomeOutbound(RecordingOutbound):
    """A boundary whose publish reports whatever the parametrized case asked for."""

    def __init__(self, outcome) -> None:
        super().__init__()
        self._raw = outcome

    def send_file(self, file_bytes: StoredFile, **kwargs):
        self.files.append((file_bytes, dict(kwargs)))
        return self._raw

    def send_image(self, image_bytes: StoredFile, **kwargs):
        self.images.append((image_bytes, dict(kwargs)))
        return self._raw


def _ctx(
    tmp_path,
    *,
    source: str = "browser",
    transport=None,
    task_id: str = "task-1",
    chat_id: int | str = 0,
    drive_root: pathlib.Path | None = None,
    outbound=None,
) -> ToolContext:
    drive_root = drive_root or tmp_path / "data"
    workspace = drive_root / "tmp" / "task-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=drive_root,
        scope=OwnerScope(owner_id="global", agent_id="main", surface_id=source, chat_id=str(chat_id)),
        transport=dict(transport or {}),
        task_id=task_id,
        task_workspace=workspace,
        outbound=outbound if outbound is not None else RecordingOutbound(),
    )


def test_failed_workspace_delivery_can_retry_the_same_unchanged_file(tmp_path):
    ctx = _ctx(tmp_path, outbound=RecordingOutbound((False, "offline")))
    path = ctx.task_workspace / "report.txt"
    path.write_text("report")
    assert "SEND_FILE_DELIVERY_ERROR" in _tool("send_file")(ctx, file_path=str(path))
    ctx.outbound = RecordingOutbound()
    assert _tool("send_file")(ctx, file_path=str(path)).startswith("OK:")
    assert outbound_bytes(ctx.outbound.files[-1][0], ctx.scope) == b"report"


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
    outbound = _FixedOutcomeOutbound(transport_result)
    ctx = _ctx(tmp_path, source="dialogs", outbound=outbound)
    workspace = ctx.task_workspace
    assert workspace is not None
    report = workspace / "report.txt"
    report.write_text("report", encoding="utf-8")

    result = _tool("send_file")(ctx, file_path=str(report), caption="report")

    assert result.startswith(expected)
    assert ctx.pending_events == []
    # The chat is no longer an argument: the boundary reads it off the scope it is handed.
    data, kwargs = outbound.files[0]
    assert outbound_bytes(data, ctx.scope) == b"report"
    assert kwargs["scope"] == ctx.scope
    assert kwargs["source"] == "dialogs"


@pytest.mark.parametrize("kind", ["document", "image"])
def test_outbound_uses_configured_attachment_limits(tmp_path, isolate_settings, kind):
    ctx = _ctx(tmp_path)
    filename, data = ("report.txt", b"12345") if kind == "document" else ("image.png", b"\x89PNG\r\n\x1a\n")
    path = ctx.task_workspace / filename
    path.write_bytes(data)
    isolate_settings.save_settings({f"OUROBOROS_ATTACHMENT_{kind.upper()}_MAX_BYTES": 4})
    result = _tool("send_file")(ctx, file_path=str(path))
    assert "4 bytes" in result
    assert ctx.outbound.files == []


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
    args, kwargs = ctx.outbound.files[0]
    assert outbound_bytes(args, ctx.scope) == b"private report"
    # The conversation travels on the scope, not as a second argument beside it.
    assert kwargs["scope"].chat_id == "peer-42"
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


def test_send_file_sends_existing_stored_attachment_by_id(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, source="browser", task_id="task-resend")
    from ouroboros.storage.chat_store import append_chat_message

    message = append_chat_message(
        ctx.drive_root,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=0,
        user_id=0,
        text="photo",
        source="browser",
        client_message_id="browser-message-1",
    )
    assert message is not None
    metadata = stored_file(
        scope=TEST_SCOPE,
        provider=get_storage_provider(),
        data=b"\x89PNG\r\n\x1a\nstored image",
        attachment_id="attachment-resend",
        filename="photo.png",
        mime="image/png",
        source="browser",
    )
    monkeypatch.chdir(ctx.task_workspace)
    # The shape ``ouroboros/agent/core.py`` binds to a run: the attachments of the
    # inbound message that started it.
    ctx = replace(ctx, attachments=[dict(metadata)])
    assert _tool("send_file")(ctx, file_path=metadata["id"]).startswith("OK:")

    historical_path = f"uploads/{metadata['storage_key']}"
    assert _tool("send_image")(ctx, file_path=historical_path).startswith("OK:")
    assert _tool("send_image")(ctx, file_path="attachment-missing").startswith("⚠️ SEND_IMAGE_NOT_FOUND:")
    assert outbound_bytes(ctx.outbound.files[0][0], ctx.scope) == b"\x89PNG\r\n\x1a\nstored image"
    assert ctx.outbound.images[0][1]["filename"] == "photo.png"
    # The union half: an id bound to the run travels with it, whatever chat the record names.
    other_chat = replace(
        _ctx(tmp_path, source="dialogs", chat_id=9, drive_root=ctx.drive_root),
        attachments=[dict(metadata)],
    )
    assert _tool("send_file")(other_chat, file_path=metadata["id"]).startswith("OK:")
    assert outbound_bytes(other_chat.outbound.files[0][0], other_chat.scope) == b"\x89PNG\r\n\x1a\nstored image"


def test_send_file_sends_earlier_attachment_of_the_same_chat(tmp_path, monkeypatch):
    """The run was given nothing, but the file belongs to the conversation it is in."""
    ctx = _ctx(tmp_path, source="browser", task_id="task-unbound", chat_id=TEST_SCOPE.chat_id)
    metadata = stored_file(
        scope=TEST_SCOPE,
        provider=get_storage_provider(),
        data=b"\x89PNG\r\n\x1a\nstored image",
        attachment_id="attachment-unbound",
        filename="photo.png",
        mime="image/png",
        source="browser",
    )
    link_file(metadata)
    monkeypatch.chdir(ctx.task_workspace)

    assert ctx.attachments == []
    assert _tool("send_file")(ctx, file_path=metadata["id"]).startswith("OK:")
    assert outbound_bytes(ctx.outbound.files[0][0], ctx.scope) == b"\x89PNG\r\n\x1a\nstored image"


def test_send_file_refuses_attachment_of_another_chat_as_absent(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, source="browser", task_id="task-other-chat", chat_id="another-chat")
    metadata = stored_file(
        scope=TEST_SCOPE,
        provider=get_storage_provider(),
        data=b"\x89PNG\r\n\x1a\nstored image",
        attachment_id="attachment-other-chat",
        filename="photo.png",
        mime="image/png",
        source="browser",
    )
    monkeypatch.chdir(ctx.task_workspace)

    result = _tool("send_file")(ctx, file_path=metadata["id"])

    # Absence, not a permission answer: a distinguishable refusal would confirm that the id
    # this run was shown is live in a chat it is not in.
    assert result.startswith("⚠️ SEND_FILE_NOT_FOUND:")
    assert "photo.png" not in result
    assert ctx.outbound.files == []


def test_send_file_refuses_file_outside_the_task_workspace(tmp_path):
    """``NOT_OWNED`` still classifies the local-path violation it was always about."""
    ctx = _ctx(tmp_path, source="dialogs")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    result = _tool("send_file")(ctx, file_path=str(outside))

    assert result.startswith("⚠️ SEND_FILE_NOT_OWNED:")
    assert ctx.outbound.files == []


def test_send_file_refuses_attachment_of_another_owner(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, source="browser", task_id="task-other-owner", chat_id=TEST_SCOPE.chat_id)
    metadata = stored_file(
        scope=scope_with(TEST_SCOPE, owner_id="someone-else"),
        provider=get_storage_provider(),
        data=b"\x89PNG\r\n\x1a\nstored image",
        attachment_id="attachment-other-owner",
        filename="photo.png",
        mime="image/png",
        source="browser",
    )
    monkeypatch.chdir(ctx.task_workspace)

    result = _tool("send_file")(ctx, file_path=metadata["id"])

    assert result.startswith("⚠️ SEND_FILE_NOT_FOUND:")
    assert ctx.outbound.files == []


def _blind_mimetypes(monkeypatch):
    from ouroboros.tools.service import messaging

    monkeypatch.setattr(messaging.mimetypes, "guess_type", lambda *_args, **_kwargs: (None, None))


@parametrize_from_dict(
    {
        "xlsx": {"filename": "book.xlsx", "mime": XLSX_MIME, "write": write_workbook},
        "docx": {"filename": "letter.docx", "mime": DOCX_MIME, "write": write_document},
        "pptx": {"filename": "deck.pptx", "mime": PPTX_MIME, "write": write_deck},
    }
)
def test_send_file_resends_a_stored_office_attachment_without_mimetypes_database(
    tmp_path, monkeypatch, filename, mime, write
):
    """An office file the user sent goes back the way it came, under its stored MIME."""
    _blind_mimetypes(monkeypatch)
    write(tmp_path / filename)
    ctx = _ctx(tmp_path, source="dialogs", task_id="task-resend")
    metadata = stored_file(
        scope=TEST_SCOPE,
        provider=get_storage_provider(),
        data=(tmp_path / filename).read_bytes(),
        attachment_id="attachment-office",
        filename=filename,
        mime=mime,
        source="dialogs",
    )
    ctx = replace(ctx, attachments=[dict(metadata)])

    result = _tool("send_file")(ctx, file_path=metadata["id"])

    assert result.startswith("OK:")
    assert ctx.outbound.files[0][1]["mime"] == mime


def test_send_file_delivers_a_deck_from_the_workspace(tmp_path):
    """A deck staged into the workspace — what WorkStation file_read hands to send_file — is deliverable."""
    ctx = _ctx(tmp_path, source="dialogs", task_id="task-deck")
    assert ctx.task_workspace is not None
    write_deck(ctx.task_workspace / "deck.pptx")

    result = _tool("send_file")(ctx, file_path=str(ctx.task_workspace / "deck.pptx"))

    assert result.startswith("OK:")
    assert ctx.outbound.files[0][1]["mime"] == PPTX_MIME


def test_send_file_sends_workspace_markdown_without_mimetypes_database(tmp_path, monkeypatch):
    _blind_mimetypes(monkeypatch)
    ctx = _ctx(tmp_path, source="dialogs")
    workspace = ctx.task_workspace
    assert workspace is not None
    report = workspace / "report.md"
    report.write_text("# report", encoding="utf-8")

    result = _tool("send_file")(ctx, file_path=str(report))

    assert result.startswith("OK:")
    assert ctx.outbound.files[0][1]["mime"] == "text/markdown"


def test_send_file_rejects_stored_attachment_with_invalid_content(tmp_path):
    ctx = _ctx(tmp_path, source="dialogs")
    metadata = stored_file(
        scope=TEST_SCOPE,
        provider=get_storage_provider(),
        data=b"\xff\x00not text",
        attachment_id="attachment-mismatch",
        filename="report.pdf",
        mime="text/plain",
        source="dialogs",
    )
    ctx = replace(ctx, attachments=[dict(metadata)])

    result = _tool("send_file")(ctx, file_path=metadata["id"])

    assert "INVALID_ATTACHMENT" in result
    assert "NOT_OWNED" not in result
    assert not ctx.outbound.files


def test_send_file_storage_failure_keeps_safe_audit_cause(tmp_path, monkeypatch):
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.tools.events import PublishedToolEvents

    ctx = _ctx(tmp_path, task_id="task-storage")
    ctx.events = PublishedToolEvents(None, audit_event_repository=sql_audit_event_repository())
    store = runtime_persistence.initialize()
    from ouroboros.tools.core import _data_write

    assert _data_write(ctx, "uploads/report.txt", "report").startswith("OK")
    provider = SimpleNamespace(
        copy_to_path=lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("storage-token\nprivate path"))
    )
    from ouroboros.storage.blob.factory import (
        install_storage_provider_builder_for_tests,
        reset_storage_provider_for_tests,
    )
    from tests.helpers.blob_double import InMemoryStorageProvider

    install_storage_provider_builder_for_tests(lambda: provider)
    reset_storage_provider_for_tests()
    try:
        result = _tool("send_file")(ctx, file_path="uploads/report.txt")
    finally:
        install_storage_provider_builder_for_tests(InMemoryStorageProvider)
        reset_storage_provider_for_tests()

    assert result.startswith("⚠️ SEND_FILE_STORAGE_ERROR:")
    audit = (
        sql_audit_event_repository(store.engine)
        .list_audit_events(event_type="artifact_materialization_error", log_stream="tools", limit=1)[0]
        .metadata
    )
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
    assert ctx.pending_events[0].transport == {
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


def test_send_file_refuses_another_owners_explicit_attachment(tmp_path):
    from tests.helpers.shared import scope_with

    ctx = _ctx(tmp_path, source="browser", task_id="task-foreign")
    metadata = stored_file(
        scope=scope_with(ctx.scope, owner_id="somebody-else"),
        provider=get_storage_provider(),
        data=b"\x89PNG\r\n\x1a\nstored image",
        attachment_id="attachment-foreign",
        filename="photo.png",
        mime="image/png",
        source="browser",
    )
    ctx = replace(ctx, attachments=[dict(metadata)])
    result = _tool("send_file")(ctx, file_path=metadata["id"])
    assert result.startswith("⚠️ SEND_FILE_")
    assert ctx.outbound.files == []
