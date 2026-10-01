from __future__ import annotations

import base64
import io
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ouroboros.gateway.browser_chat import _HISTORY_MAX_LIMIT
from ouroboros.gateway.browser_ws import BROWSER_CHAT_ID
from ouroboros.gateway.contracts import ChatHistoryResponse, UploadResponse
from ouroboros.storage.blob.factory import create_storage_provider
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.chat_store import append_chat_message
from supervisor.message_bus import LocalChatBridge
from supervisor.testing import bootstrap_supervisor_for_tests
from tests.helpers.server_client import patched_server_client

HISTORY_KEYS = set(ChatHistoryResponse.__annotations__.keys())
UPLOAD_KEYS = set(UploadResponse.__annotations__.keys())


@contextmanager
def _browser_ingress_client(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    with patched_server_client(
        monkeypatch,
        tmp_path,
        browser_enabled=True,
        reset_storage=True,
    ) as client:
        yield client, data_dir


def _seed_browser_messages(data_dir, *, count: int = 2) -> None:
    for index in range(count):
        append_chat_message(
            data_dir,
            actor_type="owner" if index % 2 == 0 else "agent",
            chat_id=BROWSER_CHAT_ID,
            user_id=BROWSER_CHAT_ID,
            text=f"message-{index}",
            source="browser",
            ts=f"2026-01-01T00:00:{index:02d}+00:00",
        )


def test_chat_history_returns_browser_messages(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        _seed_browser_messages(data_dir)
        response = client.get("/api/chat/history")
    assert response.status_code == 200
    payload = response.json()
    assert "messages" in payload
    assert set(payload.keys()).issubset(HISTORY_KEYS | {"messages"})
    assert len(payload["messages"]) == 2
    roles = {entry["role"] for entry in payload["messages"]}
    assert roles == {"user", "assistant"}
    assert all(entry["text"].startswith("message-") for entry in payload["messages"])


def test_chat_history_respects_limit(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        _seed_browser_messages(data_dir, count=5)
        response = client.get("/api/chat/history?limit=2")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["messages"]) == 2
    assert payload.get("has_more") is True
    assert payload.get("next_before_ts")


def test_chat_history_reaches_the_newest_message_in_a_long_history(monkeypatch, tmp_path):
    """A history longer than the read window must not pin the page to its oldest
    end — that is what left the browser replaying the same old conversation and
    never showing what was just said."""
    total = _HISTORY_MAX_LIMIT + 100
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        for index in range(total):
            append_chat_message(
                data_dir,
                actor_type="owner" if index % 2 == 0 else "agent",
                chat_id=BROWSER_CHAT_ID,
                user_id=BROWSER_CHAT_ID,
                text=f"message-{index:05d}",
                source="browser",
                ts=f"2026-01-01T00:00:00.{index:06d}+00:00",
            )
        payload = client.get("/api/chat/history?limit=3").json()

    assert [entry["text"] for entry in payload["messages"]] == [
        f"message-{total - 3:05d}",
        f"message-{total - 2:05d}",
        f"message-{total - 1:05d}",
    ]
    assert payload.get("has_more") is True


def test_chat_history_serves_the_newest_page_and_pages_back(monkeypatch, tmp_path):
    """The page is cut from the newest end, and ``before_ts`` walks the rest of
    the history rather than the first window the store handed back."""
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        _seed_browser_messages(data_dir, count=5)
        first = client.get("/api/chat/history?limit=2").json()
        second = client.get(f"/api/chat/history?limit=2&before_ts={first['next_before_ts']}").json()
        third = client.get(f"/api/chat/history?limit=2&before_ts={second['next_before_ts']}").json()

    assert [entry["text"] for entry in first["messages"]] == ["message-3", "message-4"]
    assert [entry["text"] for entry in second["messages"]] == ["message-1", "message-2"]
    assert [entry["text"] for entry in third["messages"]] == ["message-0"]
    assert third.get("has_more") is not True


def test_chat_history_includes_owner_messages_from_all_channels(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        append_chat_message(
            data_dir,
            actor_type="owner",
            chat_id=99,
            user_id=99,
            text="from-dialogs",
            source="dialogs",
            ts="2025-12-31T23:59:59+00:00",
        )
        _seed_browser_messages(data_dir, count=1)
        response = client.get("/api/chat/history")
    assert response.status_code == 200
    payload = response.json()
    assert [message["text"] for message in payload["messages"]] == [
        "from-dialogs",
        "message-0",
    ]


def test_chat_history_includes_assistant_reply_from_browser_task(monkeypatch, tmp_path):
    from supervisor import events, message_bus
    from supervisor.runtime_context import reset_runtime_for_tests

    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        try:
            _seed_browser_messages(data_dir, count=1)
            bridge = LocalChatBridge({})
            bootstrap_supervisor_for_tests(data_dir, bridge=bridge)

            events._handle_send_message(
                {
                    "type": "send_message",
                    "chat_id": BROWSER_CHAT_ID,
                    "text": "assistant persisted reply",
                    "log_text": "assistant persisted reply",
                    "format": "markdown",
                    "source": "browser",
                    "task_id": "task-1",
                    "ts": "2026-01-01T00:00:01+00:00",
                },
                SimpleNamespace(
                    DRIVE_ROOT=data_dir,
                    send_with_budget=message_bus.send_with_budget,
                ),
            )

            response = client.get("/api/chat/history")
        finally:
            reset_runtime_for_tests()

    assert response.status_code == 200
    payload = response.json()
    assert [(entry["role"], entry["text"]) for entry in payload["messages"]] == [
        ("user", "message-0"),
        ("assistant", "assistant persisted reply"),
    ]


def test_send_photo_is_persisted_and_restored_from_history(monkeypatch, tmp_path):
    from supervisor import events
    from supervisor.runtime_context import reset_runtime_for_tests

    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        bridge = LocalChatBridge({})
        bootstrap_supervisor_for_tests(data_dir, bridge=bridge)
        try:
            events._handle_send_photo(
                {
                    "chat_id": BROWSER_CHAT_ID,
                    "image_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode("ascii"),
                    "mime": "image/png",
                    "caption": "result",
                    "source": "browser",
                },
                SimpleNamespace(DRIVE_ROOT=data_dir, send_photo=bridge.send_photo, bridge=bridge),
            )
            history = client.get("/api/chat/history").json()
            attachment = history["messages"][0]["attachments"][0]
            image = client.get(f"/{attachment['url']}")
        finally:
            reset_runtime_for_tests()

    assert history["messages"][0]["text"] == "result"
    assert attachment["kind"] == "image"
    assert image.content == b"\x89PNG\r\n\x1a\nimage"


def test_chat_upload_round_trip_file_backend(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        response = client.post(
            "/api/chat/upload",
            files={"file": ("note #1.txt", io.BytesIO(b"hello upload"), "text/plain")},
        )
        assert response.status_code == 200
        payload = response.json()
        assert UPLOAD_KEYS.issubset(payload.keys())
        assert payload["ok"] is True
        assert payload["filename"] == "note_#1.txt"
        assert payload["display_name"] == "note_#1.txt"
        assert payload["path"] == f"api/chat/files/{payload['upload_id']}/note_%231.txt"
        assert payload["mime"] == "text/plain"
        assert payload["size"] == len(b"hello upload")
        assert payload["attachment_id"] == payload["upload_id"]
        assert payload["status"] == "draft"
        assert payload["expires_at"]
        assert payload["url"] == f"api/chat/files/{payload['attachment_id']}/note_%231.txt"
        upload_id = payload["upload_id"]

        provider = create_storage_provider(data_dir=data_dir, backend="file")
        storage_key = f"chat/{upload_id}/note_#1.txt"
        assert provider.get_bytes(SCOPE_UPLOADS, storage_key) == b"hello upload"

        download_response = client.get(f"/{payload['url']}")
        assert download_response.status_code == 200
        assert download_response.content == b"hello upload"
        assert download_response.headers["content-type"].startswith("text/plain")
        assert 'filename="note_#1.txt"' in download_response.headers["content-disposition"]

        opaque_response = client.get(f"/{payload['url']}")
        assert opaque_response.status_code == 200
        assert opaque_response.content == b"hello upload"

        delete_response = client.delete(f"/api/chat/upload?id={upload_id}")
        assert delete_response.status_code == 200
        assert delete_response.json() == {"status": "ok"}
        assert provider.get_bytes(SCOPE_UPLOADS, storage_key) is None


def test_chat_file_uses_inline_only_for_safe_image_preview(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        response = client.post(
            "/api/chat/upload",
            files={"file": ("photo.png", io.BytesIO(b"\x89PNG\r\n\x1a\n"), "image/png")},
        )
        attachment_id = response.json()["attachment_id"]

        preview = client.get(f"/api/chat/files/{attachment_id}")
        download = client.get(f"/api/chat/files/{attachment_id}?download=1")

    assert preview.headers["content-type"].startswith("image/png")
    assert preview.headers["content-disposition"].startswith("inline;")
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"


def test_chat_upload_rejects_spoofed_inline_image(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        response = client.post(
            "/api/chat/upload",
            files={"file": ("photo.png", io.BytesIO(b"<script>alert(1)</script>"), "image/png")},
        )

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "code": "invalid_attachment",
        "message": "file content does not match image type",
        "filename": "photo.png",
        "retryable": False,
    }


def test_chat_file_never_inlines_spoofed_legacy_image(monkeypatch, tmp_path):
    from ouroboros.storage.chat_attachments import store_attachment_bytes

    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        provider = create_storage_provider(data_dir=data_dir, backend="file")
        metadata = store_attachment_bytes(
            provider,
            attachment_id="legacy-spoofed-image",
            filename="photo.png",
            mime="image/png",
            data=b"<script>alert(1)</script>",
            source="browser",
            status="ready",
        )
        response = client.get(f"/api/chat/files/{metadata['id']}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_chat_history_serializes_attachments_without_storage_key(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        append_chat_message(
            data_dir,
            actor_type="agent",
            chat_id=BROWSER_CHAT_ID,
            user_id=BROWSER_CHAT_ID,
            text="Готово",
            source="browser",
            extra_metadata={
                "attachments": [
                    {
                        "id": "att-1",
                        "kind": "file",
                        "filename": "report #1.pdf",
                        "mime": "application/pdf",
                        "size": 42,
                        "status": "ready",
                        "storage_key": "attachments/att-1/report.pdf",
                    }
                ]
            },
        )

        response = client.get("/api/chat/history")

    attachment = response.json()["messages"][0]["attachments"][0]
    assert attachment == {
        "id": "att-1",
        "kind": "file",
        "filename": "report #1.pdf",
        "mime": "application/pdf",
        "size": 42,
        "status": "ready",
        "expires_at": "",
        "url": "api/chat/files/att-1/report%20%231.pdf",
    }


def test_chat_file_download_resolves_public_alias(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        provider = create_storage_provider(data_dir=data_dir, backend="file")
        provider.put_bytes(
            SCOPE_UPLOADS,
            "generated/synthetic_compare/job-1/comparison-report.xlsx",
            b"report-bytes",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        provider.put_json(
            SCOPE_UPLOADS,
            "_meta/public/comparison-report.xlsx.json",
            {
                "kind": "generated_artifact",
                "storage_key": "generated/synthetic_compare/job-1/comparison-report.xlsx",
                "display_name": "comparison-report.xlsx",
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "public_name": "comparison-report.xlsx",
            },
        )

        response = client.get("/api/chat/files/comparison-report.xlsx")

    assert response.status_code == 200
    assert response.content == b"report-bytes"
    assert "spreadsheetml.sheet" in response.headers["content-type"]
    assert 'filename="comparison-report.xlsx"' in response.headers["content-disposition"]


def test_chat_file_download_rejects_traversal(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        response = client.get("/api/chat/files/..%2Fstate%2Fsecret.txt")

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert "secret" not in response.text


def test_chat_file_content_disposition_strips_control_characters():
    from ouroboros.gateway.browser_chat import _content_disposition

    value = _content_disposition('report"\r\nX-Injected: yes.txt')

    assert "\r" not in value and "\n" not in value
    assert value.count('"') == 2


def test_chat_upload_delete_accepts_legacy_path_body(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        response = client.post(
            "/api/chat/upload",
            files={"file": ("note.txt", io.BytesIO(b"delete me"), "text/plain")},
        )
        payload = response.json()
        provider = create_storage_provider(data_dir=data_dir, backend="file")
        storage_key = f"chat/{payload['upload_id']}/note.txt"
        assert provider.get_bytes(SCOPE_UPLOADS, storage_key) == b"delete me"
        generated_key = "generated/synthetic_compare/job/note.txt"
        provider.put_bytes(SCOPE_UPLOADS, generated_key, b"report", content_type="text/plain")
        provider.put_json(
            SCOPE_UPLOADS,
            "_meta/public/note.txt.json",
            {"storage_key": generated_key, "display_name": "note.txt"},
        )

        generated_delete = client.request("DELETE", "/api/chat/upload", json={"path": "note.txt"})

        delete_response = client.request(
            "DELETE",
            "/api/chat/upload",
            json={"path": payload["path"]},
        )

    assert delete_response.status_code == 200
    assert generated_delete.status_code in {400, 404}
    assert delete_response.json() == {"status": "ok"}
    assert provider.get_bytes(SCOPE_UPLOADS, storage_key) is None
    assert provider.get_json(SCOPE_UPLOADS, "_meta/public/note.txt.json")["storage_key"] == generated_key


def test_bind_attachments_rejects_expired_draft(tmp_path):
    from ouroboros.storage.chat_attachments import (
        attachment_meta_key,
        bind_attachments,
        get_attachment,
        store_attachment_bytes,
    )

    provider = create_storage_provider(data_dir=tmp_path, backend="file")
    store_attachment_bytes(
        provider,
        attachment_id="expired-attachment",
        filename="note.txt",
        mime="text/plain",
        data=b"hello",
        source="browser",
        status="draft",
    )
    metadata = get_attachment(provider, "expired-attachment")
    assert metadata is not None
    metadata["expires_at"] = "2020-01-01T00:00:00+00:00"
    provider.atomic_put_json(
        SCOPE_UPLOADS,
        attachment_meta_key("expired-attachment"),
        metadata,
    )

    with pytest.raises(ValueError, match="expired"):
        bind_attachments(provider, ["expired-attachment"], message_id="mid-1")

    persisted = get_attachment(provider, "expired-attachment")
    assert persisted is not None
    assert persisted["status"] == "draft"


def test_legacy_filename_scoped_attachment_download(monkeypatch, tmp_path):
    attachment_id = "a" * 32
    storage_key = f"chat/{attachment_id}/note.txt"
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        provider = create_storage_provider(data_dir=data_dir, backend="file")
        provider.put_bytes(SCOPE_UPLOADS, storage_key, b"legacy")
        provider.atomic_put_json(
            SCOPE_UPLOADS,
            f"_meta/{storage_key}.json",
            {
                "id": attachment_id,
                "storage_key": storage_key,
                "filename": "note.txt",
                "mime": "text/plain",
                "status": "ready",
                "expires_at": "2030-01-01T00:00:00+00:00",
            },
        )
        response = client.get(f"/api/chat/files/{attachment_id}/note.txt")

    assert response.status_code == 200
    assert response.content == b"legacy"


@pytest.mark.parametrize(
    "status_code,payload,files",
    [
        (400, {"ok": False, "error": "missing file upload"}, None),
        (400, {"ok": False, "error": "invalid multipart request"}, "not-multipart"),
    ],
)
def test_chat_upload_rejects_malformed_requests(monkeypatch, tmp_path, status_code, payload, files):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        if files == "not-multipart":
            response = client.post("/api/chat/upload", data=b"not multipart", headers={"content-type": "text/plain"})
        else:
            response = client.post("/api/chat/upload")
    assert response.status_code == status_code
    body = response.json()
    assert body["ok"] is False
    assert "error" in body
    assert "Traceback" not in body["error"]


def test_chat_upload_rejects_oversize(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        with patch("ouroboros.gateway.browser_chat.CHAT_UPLOAD_MAX_BYTES", 8):
            response = client.post(
                "/api/chat/upload",
                files={"file": ("big.bin", io.BytesIO(b"x" * 32), "application/octet-stream")},
            )
    assert response.status_code == 413
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "attachment_too_large"


def test_chat_upload_stops_chunked_multipart_before_full_spool(monkeypatch, tmp_path):
    import asyncio

    from starlette.requests import Request

    from ouroboros.gateway import browser_chat

    boundary = "upload-boundary"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="big.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + (b"x" * 100_000)
        + f"\r\n--{boundary}--\r\n".encode()
    )
    offset = 0
    consumed = 0

    async def receive():
        nonlocal offset, consumed
        chunk = body[offset : offset + 1024]
        offset += len(chunk)
        consumed += len(chunk)
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": offset < len(body),
        }

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat/upload",
        "query_string": b"",
        "headers": [
            (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
        ],
        "app": SimpleNamespace(state=SimpleNamespace(drive_root=tmp_path)),
    }
    request = Request(scope, receive)

    with patch("ouroboros.gateway.browser_chat.CHAT_UPLOAD_MAX_BYTES", 8):
        response = asyncio.run(browser_chat.api_chat_upload(request))

    assert response.status_code == 413
    assert consumed < len(body)


def test_chat_upload_rejects_invalid_mime(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        response = client.post(
            "/api/chat/upload",
            files={"file": ("evil.html", io.BytesIO(b"<script>"), "text/html")},
        )
        octet = client.post(
            "/api/chat/upload",
            files={"file": ("note.txt", io.BytesIO(b"text"), "application/octet-stream")},
        )
    assert response.status_code == 415
    assert octet.status_code == 415
    body = response.json()
    assert body == {
        "ok": False,
        "code": "attachment_type_not_allowed",
        "message": "unsupported file format",
        "filename": "evil.html",
        "retryable": False,
    }


def test_chat_upload_delete_requires_upload_id(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        response = client.delete("/api/chat/upload")
    assert response.status_code == 400
    assert response.json()["ok"] is False


@pytest.mark.parametrize("upload_id", ["../etc/passwd", "not-hex", ""])
def test_chat_upload_delete_rejects_invalid_upload_id(monkeypatch, tmp_path, upload_id):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        response = client.delete(f"/api/chat/upload?id={upload_id}")
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_chat_upload_round_trip_s3_backend(monkeypatch, tmp_path):
    pytest.importorskip("moto")
    import boto3
    from moto import mock_aws

    @mock_aws
    def _run() -> None:
        bucket = "ouroboros-chat-upload"
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
        monkeypatch.setenv("OUROBOROS_S3_BUCKET", bucket)
        monkeypatch.setenv("OUROBOROS_S3_REGION", "us-east-1")
        monkeypatch.setenv("OUROBOROS_STORAGE_BACKEND", "s3")
        from tests.conftest import refresh_test_config

        refresh_test_config()
        with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
            response = client.post(
                "/api/chat/upload",
                files={"file": ("photo.png", io.BytesIO(b"\x89PNG\r\n\x1a\n"), "image/png")},
            )
            assert response.status_code == 200
            payload = response.json()
            upload_id = payload["upload_id"]
            provider = create_storage_provider(data_dir=data_dir, backend="s3")
            assert provider.get_bytes(SCOPE_UPLOADS, f"chat/{upload_id}/photo.png") is not None
            download_response = client.get(f"/api/chat/files/{upload_id}/photo.png")
            assert download_response.status_code == 200
            assert download_response.content == b"\x89PNG\r\n\x1a\n"

    _run()
