"""Compatibility coverage for send_photo and image MIME detection."""

import base64
from contextlib import nullcontext

import pytest
from sqlalchemy import text

from new_core.identity import OwnerScope
from ouroboros.contracts.attachments import StoredFile
from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.chat_attachments import cleanup_expired_attachments, get_attachment
from ouroboros.storage.file_catalog import FileFailure, FileTransferPending
from ouroboros.storage.file_transfers import get_transfer_limits
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.tools.core import _send_photo, get_tools
from ouroboros.tools.service.messaging import _send_image, send_image_data
from supervisor import message_bus
from tests.helpers.fake_gateway import RecordingOutbox
from tests.helpers.files import interrupted_upload, outbound_bytes, stored_file
from tests.helpers.outbound import RecordingOutbound
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, FakeCancellation, make_tool_ctx


def _make_ctx(tmp_path, chat_id=0, source="browser"):
    workspace = tmp_path / "data" / "tmp" / "task-workspace"
    workspace.mkdir(parents=True)
    return make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=tmp_path / "data",
        scope=OwnerScope(owner_id="global", agent_id="main", surface_id=source, chat_id=str(chat_id)),
        task_workspace=workspace,
        outbound=RecordingOutbound(),
    )


class TestSendPhotoFilePath:
    def test_file_path_reads_png(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        png = ctx.task_workspace / "test.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        result = _send_photo(ctx, file_path=str(png), caption="test shot")

        assert result.startswith("OK:")
        args, kwargs = ctx.outbound.images[0]
        # The chat is not an argument any more; it is read off the scope.
        assert outbound_bytes(args, ctx.scope).startswith(b"\x89PNG")
        assert kwargs["scope"] == ctx.scope
        assert kwargs["mime"] == "image/png"
        assert kwargs["source"] == "browser"

    def test_no_input_returns_error(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        result = _send_photo(ctx)
        assert "Provide either" in result


class TestSendPhotoBase64Fallback:
    def test_base64_is_stored_before_outbound(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200).decode()
        result = _send_photo(ctx, image_base64=b64)
        assert "OK" in result
        assert len(ctx.outbound.images) == 1
        image, kwargs = ctx.outbound.images[0]
        assert isinstance(image, StoredFile)
        assert outbound_bytes(image, ctx.scope) == base64.b64decode(b64)
        assert kwargs["scope"] == ctx.scope

    def test_last_screenshot_sentinel_uses_browser_state(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200).decode()
        ctx.browser_state.last_screenshot_b64 = b64

        result = _send_photo(ctx, image_base64="__last_screenshot__")

        assert "OK" in result
        assert len(ctx.outbound.images) == 1

    def test_schema_documents_last_screenshot_sentinel(self):
        send_photo = next(tool for tool in get_tools() if tool.name == "send_photo")
        image_base64 = send_photo.schema.parameters["properties"]["image_base64"]

        assert "__last_screenshot__" in send_photo.schema.description
        assert "__last_screenshot__" in image_base64["description"]


@parametrize_from_dict({"False": {"insert_conflict": False}, "True": {"insert_conflict": True}})
@parametrize_from_dict({"False": {"existing": False}, "True": {"existing": True}})
def test_send_photo_does_not_broadcast_when_history_persistence_fails(
    monkeypatch,
    tmp_path,
    insert_conflict,
    existing,
):
    ctx = _make_ctx(tmp_path)
    content = b"\x89PNG\r\n\x1a\npayload"
    metadata = stored_file(data=content, filename="image.png", mime="image/png") if existing else None
    append = (
        (lambda *_args, **_kwargs: None)
        if insert_conflict
        else (lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("database unavailable")))
    )
    monkeypatch.setattr(message_bus, "append_chat_message", append)
    outbox = RecordingOutbox()
    ctx.outbound = message_bus.LocalChatBridge(outbox, ctx.drive_root)

    if existing:
        result = ctx.outbound.send_image(
            StoredFile(metadata["id"]), filename="image.png", source="browser", scope=ctx.scope
        )
        assert result == (False, "failed to persist attachment")
    else:
        assert "SEND_IMAGE_DELIVERY_ERROR" in send_image_data(ctx, content, caption="")

    assert outbox.published == []
    provider = get_storage_provider()
    cleanup_expired_attachments(provider, cancellation=FakeCancellation())
    remaining = [
        provider.get_bytes(TEST_SCOPE, SCOPE_UPLOADS, key)
        for key in provider.list_keys(TEST_SCOPE, SCOPE_UPLOADS, "chat/")
        if provider.get_bytes(TEST_SCOPE, SCOPE_UPLOADS, key)
    ]
    assert remaining == ([content] if existing else [])
    if existing:
        assert get_attachment(ctx.scope, metadata["id"])["status"] == "ready"


def test_interrupted_image_storage_keeps_bytes_for_retry_and_eventual_cleanup(tmp_path):
    ctx = _make_ctx(tmp_path)
    with interrupted_upload() as provider:
        result = send_image_data(ctx, b"\x89PNG\r\n\x1a\npayload", caption="")
        assert "SEND_IMAGE_STORAGE_ERROR" in result
        assert ctx.outbound.images == []
        with runtime_persistence.current.engine.begin() as conn:
            assert conn.execute(text("SELECT status, claim_token FROM files")).all() == [("staging", None)]
            conn.execute(text("UPDATE files SET expires_at='2000-01-01'"))
        assert next(ctx.task_workspace.glob("*.png")).read_bytes() == b"\x89PNG\r\n\x1a\npayload"
        cleanup_expired_attachments(provider, cancellation=FakeCancellation())
        assert all(
            not provider.get_bytes(ctx.scope, SCOPE_UPLOADS, key)
            for key in provider.list_keys(ctx.scope, SCOPE_UPLOADS, "chat/")
        )


@parametrize_from_dict({"offline": {"detail": "offline"}, "1": {"detail": DELIVERY_UNKNOWN}})
def test_failed_image_handoff_preserves_only_an_unknown_delivery(tmp_path, detail):
    ctx = _make_ctx(tmp_path)
    ctx.outbound = RecordingOutbound((False, detail))
    result = send_image_data(ctx, b"\x89PNG\r\n\x1a\npayload", caption="")
    assert result.startswith("⚠️ SEND_IMAGE_DELIVERY_")
    assert len(ctx.outbound.images) == 1
    media, _ = ctx.outbound.images[0]
    metadata = get_attachment(ctx.scope, media.file_id)
    assert metadata["status"] == ("ready" if detail == DELIVERY_UNKNOWN else "deleting")
    if detail == DELIVERY_UNKNOWN:
        assert "do not retry automatically" in result


def test_image_publish_failure_preserves_history_and_its_file(tmp_path):
    ctx = _make_ctx(tmp_path)
    outbox = RecordingOutbox()
    outbox.failure = OSError("outbox unavailable")
    ctx.outbound = message_bus.LocalChatBridge(outbox, ctx.drive_root)
    content = b"\x89PNG\r\n\x1a\npayload"
    assert "SEND_IMAGE_DELIVERY_ERROR" in send_image_data(ctx, content, caption="image")
    assert outbox.published == []
    [message] = runtime_persistence.current.conversations_repo.list_messages(scope=ctx.scope)
    media = StoredFile(message.attachments[0]["id"])
    assert get_attachment(ctx.scope, media.file_id)["status"] == "ready"
    cleanup_expired_attachments(get_storage_provider(), cancellation=FakeCancellation())
    assert outbound_bytes(media, ctx.scope) == content


@parametrize_from_dict(
    {
        "unknown_format": {"content": b"not an image", "error": "Unsupported image format"},
        "oversized_image": {"content": b"\x89PNG\r\n\x1a\npayload", "error": "SEND_IMAGE_TOO_LARGE"},
    }
)
def test_new_image_validation_rejects_before_storage(tmp_path, isolate_settings, content, error):
    ctx = _make_ctx(tmp_path)
    isolate_settings.save_settings({"OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES": 8})
    assert error in send_image_data(ctx, content, caption="")
    assert ctx.outbound.images == []
    assert get_storage_provider().list_keys(ctx.scope, SCOPE_UPLOADS, "chat/") == []


@pytest.mark.parametrize("during_delivery", [False, True])
def test_generated_image_survives_capacity_refusal_and_can_be_sent_by_path(tmp_path, monkeypatch, during_delivery):
    monkeypatch.setenv("BROWSER_INGRESS_ENABLED", "false")
    monkeypatch.setenv("OUROBOROS_FILE_OWNER_TRANSFERS", "1")
    ctx = _make_ctx(tmp_path)
    ctx.task_id = "image-task"
    content = b"\x89PNG\r\n\x1a\npayload"

    class BusyOutbound(RecordingOutbound):
        def send_image(self, *args, **kwargs):
            raise FileTransferPending("busy", FileFailure.OWNER_CAPACITY)

    if during_delivery:
        ctx.outbound = BusyOutbound()
    with nullcontext() if during_delivery else get_transfer_limits().slot(ctx.scope):
        result = send_image_data(ctx, content, caption="image")
    paths = list(ctx.task_workspace.glob("*.png"))
    assert len(paths) == 1
    assert paths[0].read_bytes() == content
    assert str(paths[0]) in result and "retryable=true" in result
    ctx.outbound = RecordingOutbound()
    assert _send_image(ctx, str(paths[0])).startswith("OK:")
    assert outbound_bytes(ctx.outbound.images[0][0], ctx.scope) == content
