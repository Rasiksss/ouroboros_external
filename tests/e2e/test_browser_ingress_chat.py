from __future__ import annotations

import asyncio
import base64
import io
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from infra.async_engine import async_engine_factory
from ouroboros.agent_events.interfaces import OutboundText, PhotoSendRequested
from ouroboros.contracts.tool_confirmation import CONFIRMATION_TRANSPORT_KEY
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.chat_store import append_chat_message, list_owner_messages
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.store import load_settings
from tests.helpers.blob_double import recording_blob_double
from tests.helpers.files import stored_file
from tests.helpers.runtime_store import claim_task, park_run
from tests.helpers.scope import scope_for
from tests.helpers.server_client import patched_server_client
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.supervisor import bootstrap_supervisor_for_tests
from transport.browser import chat as browser_chat
from transport.browser.contracts import ChatHistoryResponse, UploadResponse
from transport.browser.ws import BROWSER_CHAT_ID

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
            scope=TEST_SCOPE,
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


def test_chat_history_takes_its_window_from_the_newest_end(monkeypatch, tmp_path):
    """A window cut from the oldest end never reaches what the user just said."""
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        _seed_browser_messages(data_dir, count=5)
        response = client.get("/api/chat/history?limit=2")

    assert response.status_code == 200
    assert [message["text"] for message in response.json()["messages"]] == ["message-3", "message-4"]


def test_chat_history_pages_backwards_from_the_newest_to_the_oldest(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        _seed_browser_messages(data_dir, count=12)
        pages: list[list[str]] = []
        params: dict[str, object] = {"limit": 3}
        while len(pages) < 12:
            payload = client.get("/api/chat/history", params=params).json()
            pages.append([message["text"] for message in payload["messages"]])
            if not payload.get("has_more"):
                break
            params = {"limit": 3, "before_ts": payload["next_before_ts"]}

    assert pages[0] == ["message-9", "message-10", "message-11"]
    assert [text for page in reversed(pages) for text in page] == [f"message-{index}" for index in range(12)]


def test_chat_history_includes_owner_messages_from_all_channels(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        append_chat_message(
            data_dir,
            scope=TEST_SCOPE,
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
            client_message_id = "browser-client-1"
            transport = {"kind": "browser", "source_message_id": client_message_id}
            append_chat_message(
                data_dir,
                scope=TEST_SCOPE,
                actor_type="owner",
                chat_id=BROWSER_CHAT_ID,
                user_id=BROWSER_CHAT_ID,
                text="message-0",
                source="browser",
                client_message_id=client_message_id,
                transport=transport,
                ts="2026-01-01T00:00:00+00:00",
            )
            bootstrap_supervisor_for_tests(data_dir)

            events._handle_send_message(
                OutboundText(
                    scope=scope_for("browser", BROWSER_CHAT_ID),
                    text="assistant persisted reply",
                    log_text="assistant persisted reply",
                    fmt="markdown",
                    source="browser",
                    transport=transport,
                    task_id="task-1",
                    ts="2026-01-01T00:00:01+00:00",
                ),
                SimpleNamespace(
                    DRIVE_ROOT=data_dir,
                    send_with_budget=message_bus.send_with_budget,
                ),
            )

            user_message, assistant_message = list_owner_messages(data_dir, scope=TEST_SCOPE)
            assert user_message.source_message_id == client_message_id
            assert assistant_message.metadata["transport"]["source_message_id"] == client_message_id
            assert not assistant_message.source_message_id
            assert user_message.message_id != assistant_message.message_id
            response = client.get("/api/chat/history")
        finally:
            reset_runtime_for_tests()

    assert response.status_code == 200
    payload = response.json()
    assert [(entry["role"], entry["text"], entry["client_message_id"]) for entry in payload["messages"]] == [
        ("user", "message-0", client_message_id),
        ("assistant", "assistant persisted reply", client_message_id),
    ]


@pytest.mark.parametrize(
    "actor_type,source,transport",
    [
        ("owner", "browser", {"source_message_id": "request-1"}),
        ("system", "browser", {"source_message_id": "request-1"}),
        ("agent", "dialogs", {"source_message_id": "request-1"}),
        ("agent", "browser", {}),
        ("agent", "browser", {"source_message_id": ""}),
        ("agent", "browser", None),
        ("agent", "browser", "unexpected"),
        ("agent", "browser", ["unexpected"]),
    ],
)
def test_chat_history_preserves_ids_without_browser_reply_link(monkeypatch, tmp_path, actor_type, source, transport):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        expected_ids = []
        for client_message_id in ("message-1", ""):
            message = append_chat_message(
                data_dir,
                scope=TEST_SCOPE,
                actor_type=actor_type,
                chat_id=BROWSER_CHAT_ID,
                user_id=BROWSER_CHAT_ID,
                text="stored message",
                source=source,
                client_message_id=client_message_id,
                extra_metadata={"transport": transport},
            )
            assert message is not None
            expected_ids.append(client_message_id or message.message_id)
        response = client.get("/api/chat/history")

    assert response.status_code == 200
    assert [entry["client_message_id"] for entry in response.json()["messages"]] == expected_ids


_ASKED = {
    "confirmation_id": "17da0cd4aaaa4bbb8ccc9ddd0eee1fff",
    "task_id": "task1",
    "tool": "data_write",
    "arguments_preview": '{"path": "uploads/probe.txt"}',
    "arguments_truncated": False,
    "question": "Подтвердите действие: data_write [17da0cd4]",
}


def _seed_question(data_dir) -> None:
    append_chat_message(
        data_dir,
        scope=TEST_SCOPE,
        actor_type="agent",
        chat_id=BROWSER_CHAT_ID,
        user_id=BROWSER_CHAT_ID,
        text=_ASKED["question"],
        source="browser",
        task_id=_ASKED["task_id"],
        transport={CONFIRMATION_TRANSPORT_KEY: _ASKED},
    )


def test_chat_history_carries_the_open_request_on_the_questions_row(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        claim_task(runtime_persistence.current)
        park_run(runtime_persistence.current, confirmation_id=_ASKED["confirmation_id"])
        _seed_question(data_dir)
        row = client.get("/api/chat/history").json()["messages"][-1]
    expected = {key: value for key, value in _ASKED.items() if key != "question"}
    assert row["confirmation"] == {**expected, "open": True}


def test_chat_history_marks_a_request_nobody_is_parked_on_as_closed(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        _seed_question(data_dir)
        row = client.get("/api/chat/history").json()["messages"][-1]
    assert row["confirmation"]["open"] is False


def test_send_photo_is_persisted_and_restored_from_history(monkeypatch, tmp_path):
    from supervisor import events
    from supervisor.runtime_context import reset_runtime_for_tests

    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        bridge = bootstrap_supervisor_for_tests(data_dir).bridge
        try:
            events._handle_send_photo(
                PhotoSendRequested(
                    scope=scope_for("browser", BROWSER_CHAT_ID),
                    image_base64=base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode("ascii"),
                    mime="image/png",
                    caption="result",
                    source="browser",
                ),
                SimpleNamespace(DRIVE_ROOT=data_dir, bridge=bridge),
            )
            history = client.get("/api/chat/history").json()
            attachment = history["messages"][0]["attachments"][0]
            image = client.get(f"/{attachment['url']}")
        finally:
            reset_runtime_for_tests()

    assert history["messages"][0]["text"] == "result"
    assert attachment["kind"] == "image"
    assert image.content == b"\x89PNG\r\n\x1a\nimage"


def test_chat_upload_round_trip(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
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

        provider = get_storage_provider()
        storage_key = f"chat/{upload_id}/note_#1.txt"
        assert provider.get_bytes(TEST_SCOPE, SCOPE_UPLOADS, storage_key) == b"hello upload"

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
        assert client.get(f"/{payload['url']}").status_code == 404


def test_concurrent_uploads_keep_executor_free_and_get_full_parse_budget(monkeypatch, tmp_path):
    import asyncio

    from anyio.from_thread import start_blocking_portal
    from openpyxl import Workbook

    from ouroboros.storage import attachment_validation as attachments

    workbook = Workbook()
    workbook.active.append(["Report", 123])
    content = io.BytesIO()
    workbook.save(content)
    workbook.close()
    data = content.getvalue()
    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    upload_count = 2
    ready = threading.Event()
    parser_slot = attachments._PARSER_SLOT
    queued_seconds = 0.0
    monkeypatch.setenv("OUROBOROS_ATTACHMENT_PARSE_TIMEOUT_SEC", "5")
    monkeypatch.setenv("OUROBOROS_FILE_OWNER_TRANSFERS", "3")

    def wait_for_slot(*args, **kwargs):
        ready.set()
        return parser_slot.acquire(*args, **kwargs)

    monkeypatch.setattr(
        attachments, "_PARSER_SLOT", SimpleNamespace(acquire=wait_for_slot, release=parser_slot.release)
    )
    monkeypatch.setattr(attachments, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + queued_seconds))
    with (
        _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir),
        ThreadPoolExecutor(upload_count) as pool,
        start_blocking_portal() as portal,
    ):
        client.portal = portal
        client.portal.call(lambda: asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(1)))
        with parser_slot:
            pending = [
                pool.submit(client.post, "/api/chat/upload", files={"file": (f"book {i}.xlsx", data, mime)})
                for i in range(upload_count)
            ]
            assert ready.wait(timeout=10)
            probe = client.portal.start_task_soon(asyncio.to_thread, lambda: "responsive")
            assert probe.result(timeout=1) == "responsive"
            queued_seconds = 6.0
        responses = [future.result(timeout=15) for future in pending]
        assert [response.status_code for response in responses] == [200] * upload_count


@pytest.mark.parametrize(
    ("failure", "filename", "status", "code", "retryable", "reason"),
    [
        ("busy", "report #1.txt", 503, "attachment_parser_unavailable", True, "waiting for a parser slot timed out"),
        ("timeout", "report #1.txt", 503, "attachment_parser_unavailable", True, "attachment parsing timed out"),
        ("invalid", "report #1.xlsx", 422, "invalid_attachment", False, "file is not a valid OOXML package"),
        ("invalid", "report #1.json", 422, "invalid_attachment", False, "Expecting value: line 1 column 1 (char 0)"),
        ("forbidden", "report #1.exe", 415, "attachment_type_not_allowed", False, "unsupported file format"),
    ],
)
def test_chat_upload_reports_parser_availability_and_safe_filename(
    monkeypatch, tmp_path, failure, filename, status, code, retryable, reason
):
    from ouroboros.storage import attachment_validation as attachments

    if failure == "busy":
        monkeypatch.setenv("OUROBOROS_ATTACHMENT_SLOT_TIMEOUT_SEC", "0")
    elif failure == "timeout":
        monkeypatch.setenv("OUROBOROS_ATTACHMENT_PARSE_TIMEOUT_SEC", "1")
        process_type = attachments.multiprocessing.get_context("spawn").Process
        start = process_type.start
        elapsed = 0.0
        monkeypatch.setattr(attachments, "time", SimpleNamespace(monotonic=lambda: elapsed))

        def complete_after_deadline(process):
            nonlocal elapsed
            start(process)
            process.join(timeout=10)
            assert process.exitcode == 0
            elapsed = 2.0

        monkeypatch.setattr(process_type, "start", complete_after_deadline)
    mime = attachments.attachment_mime_for_suffix(filename) or "application/octet-stream"
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        with attachments._PARSER_SLOT if failure == "busy" else nullcontext():
            response = client.post(
                "/api/chat/upload", files={"file": (f"folder/{filename}", b"PK corrupt package", mime)}
            )

    assert response.status_code == status
    assert response.json() == {
        "ok": False,
        "code": code,
        "message": reason,
        "filename": filename.replace(" ", "_"),
        "retryable": retryable,
    }
    assert not list(tmp_path.glob("ouroboros-upload-*"))


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

    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        provider = get_storage_provider()
        metadata = stored_file(
            scope=TEST_SCOPE,
            provider=provider,
            data=b"<script>alert(1)</script>",
            attachment_id="legacy-spoofed-image",
            filename="photo.png",
            mime="image/png",
            source="browser",
        )
        response = client.get(f"/api/chat/files/{metadata['id']}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_chat_history_serializes_attachments_without_storage_key(monkeypatch, tmp_path):
    from tests.helpers.files import stored_file

    with _browser_ingress_client(monkeypatch, tmp_path) as (client, data_dir):
        file = stored_file(filename="report #1.txt")
        append_chat_message(
            data_dir,
            scope=TEST_SCOPE,
            actor_type="agent",
            chat_id=BROWSER_CHAT_ID,
            user_id=BROWSER_CHAT_ID,
            text="Done",
            source="browser",
            extra_metadata={"attachments": [file]},
        )
        attachment = client.get("/api/chat/history").json()["messages"][0]["attachments"][0]
    assert attachment == {
        "id": file["id"],
        "kind": "file",
        "filename": "report #1.txt",
        "mime": "text/plain",
        "size": 5,
        "status": "ready",
        "expires_at": "",
        "url": f"api/chat/files/{file['id']}/report%20%231.txt",
    }


@pytest.mark.parametrize("filename,mime", [("report.txt", "text/plain"), ("image.webp", "image/webp")])
def test_chat_download_enforces_lowered_limit_after_restart(monkeypatch, tmp_path, filename, mime):
    from sqlalchemy import text

    setting = f"OUROBOROS_ATTACHMENT_{'IMAGE' if mime.startswith('image/') else 'DOCUMENT'}_MAX_BYTES"
    monkeypatch.setenv(setting, "16")
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        provider = get_storage_provider()
        metadata = stored_file(
            scope=TEST_SCOPE,
            provider=provider,
            data=b"RIFF1234WEBP",
            attachment_id="lowered-limit",
            filename=filename,
            mime=mime,
            source="browser",
        )
        url = f"/api/chat/files/{metadata['id']}"
        assert client.get(url).content == b"RIFF1234WEBP"
        with runtime_persistence.current.engine.begin() as conn:
            conn.execute(text("UPDATE files SET size=1 WHERE id=:id"), {"id": metadata["id"]})

    monkeypatch.setenv(setting, "4")
    with recording_blob_double() as recorder:
        with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
            response = client.get(url)
    assert response.status_code == 413
    assert response.json()["code"] == "attachment_too_large"
    assert metadata["storage_key"] not in recorder.unbounded_reads


def test_chat_file_download_refuses_bare_filename(monkeypatch, tmp_path):

    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        provider = get_storage_provider()
        stored_file(
            scope=TEST_SCOPE,
            provider=provider,
            data=b"salary-review-bytes",
            attachment_id="a" * 32,
            filename="report.md",
            mime="text/markdown",
            source="browser",
        )
        response = client.get("/api/chat/files/report.md")

    assert response.status_code == 404
    assert b"salary-review-bytes" not in response.content


def test_chat_file_download_no_longer_serves_an_object_without_a_record(monkeypatch, tmp_path):
    """An object with no ``_meta/`` record names no owner, so nothing can be compared."""
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        provider = get_storage_provider()
        provider.put_bytes(
            TEST_SCOPE,
            SCOPE_UPLOADS,
            "generated/synthetic_compare/job-1/comparison-report.xlsx",
            b"report-bytes",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        provider.put_json(
            TEST_SCOPE,
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

        alias = client.get("/api/chat/files/comparison-report.xlsx")
        generated = client.get("/api/chat/files/generated/synthetic_compare/job-1/comparison-report.xlsx")

    assert (alias.status_code, generated.status_code) == (404, 404)
    assert b"report-bytes" not in alias.content


def test_chat_file_download_rejects_traversal(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        response = client.get("/api/chat/files/..%2Fstate%2Fsecret.txt")

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert "secret" not in response.text


def test_chat_upload_delete_accepts_legacy_path_body(monkeypatch, tmp_path):
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        response = client.post(
            "/api/chat/upload",
            files={"file": ("note.txt", io.BytesIO(b"delete me"), "text/plain")},
        )
        payload = response.json()
        provider = get_storage_provider()
        storage_key = f"chat/{payload['upload_id']}/note.txt"
        assert provider.get_bytes(TEST_SCOPE, SCOPE_UPLOADS, storage_key) == b"delete me"
        generated_key = "generated/synthetic_compare/job/note.txt"
        provider.put_bytes(TEST_SCOPE, SCOPE_UPLOADS, generated_key, b"report", content_type="text/plain")
        provider.put_json(
            TEST_SCOPE,
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
        assert client.get(f"/api/chat/files/{payload['attachment_id']}").status_code == 404

    assert delete_response.status_code == 200
    assert generated_delete.status_code in {400, 404}
    assert delete_response.json() == {"status": "ok"}
    assert provider.get_json(TEST_SCOPE, SCOPE_UPLOADS, "_meta/public/note.txt.json")["storage_key"] == generated_key


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


@pytest.mark.parametrize(
    "filename,mime,data,document_limit,image_limit,status",
    [
        ("../big.txt", "text/plain", b"x" * 32, 8, 70000, 413),
        ("picture.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"x" * 65992, 4, 70000, 200),
        ("picture.png", "image/png", b"\x89PNG\r\n\x1a\nx", 70000, 8, 413),
        ("picture.webp", "", b"RIFF1234WEBP", 4, 70000, 200),
    ],
)
def test_chat_upload_applies_format_limit_without_host_mime_database(
    monkeypatch, tmp_path, filename, mime, data, document_limit, image_limit, status
):
    import mimetypes

    monkeypatch.setattr(mimetypes, "guess_type", lambda *_args, **_kwargs: (None, None))
    monkeypatch.setenv("OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES", str(document_limit))
    monkeypatch.setenv("OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES", str(image_limit))
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir):
        response = client.post("/api/chat/upload", files={"file": (filename, data, mime)})
    assert response.status_code == status
    body = response.json()
    if status == 200:
        assert body["size"] == len(data)
        assert body["mime"] == (mime or "image/webp")
    else:
        assert body["ok"] is body["retryable"] is False
        assert body["code"] == "attachment_too_large"
        assert body["filename"] == filename.removeprefix("../")
        assert "8 bytes" in body["message"]


def test_chat_upload_stops_chunked_multipart_before_full_spool(monkeypatch, tmp_path, isolate_settings):
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

    isolate_settings.save_settings(
        {"OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES": 8, "OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES": 16}
    )

    async def upload():
        async_engine_factory.initialize_beside_store(load_settings())
        try:
            return await browser_chat.api_chat_upload(request)
        finally:
            await async_engine_factory.dispose()

    response = asyncio.run(upload())

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


@contextmanager
def _trusted_identity():
    """The resolver the multi-user composition root installs, restored afterwards."""
    from tests.helpers.agent_registry import FakeAgentRegistry
    from transport.browser.identity import TrustedHeaderIdentity, browser_identity

    previous = browser_identity.resolver
    browser_identity.initialize(TrustedHeaderIdentity(FakeAgentRegistry()))
    try:
        yield
    finally:
        browser_identity.initialize(previous)


def _headers(owner: str, chat: int = 7) -> dict[str, str]:
    from transport.browser.identity import CHAT_HEADER_NAME, OWNER_HEADER_NAME

    return {OWNER_HEADER_NAME: owner, CHAT_HEADER_NAME: str(chat)}


def test_chat_file_download_refuses_another_owners_attachment(monkeypatch, tmp_path):
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir), _trusted_identity():
        uploaded = client.post(
            "/api/chat/upload",
            files={"file": ("salary.txt", io.BytesIO(b"alice payroll"), "text/plain")},
            headers=_headers("alice"),
        )
        assert uploaded.status_code == 200
        url = uploaded.json()["url"]

        mine = client.get(f"/{url}", headers=_headers("alice"))
        theirs = client.get(f"/{url}", headers=_headers("bob"))
        unidentified = client.get(f"/{url}")

    assert mine.status_code == 200
    assert mine.content == b"alice payroll"
    # Absent, not forbidden: a distinguishable refusal is an existence oracle over
    # another owner's attachment ids.
    assert theirs.status_code == 404
    assert b"alice payroll" not in theirs.content
    assert unidentified.status_code == 403


def test_chat_upload_delete_refuses_another_owners_draft(monkeypatch, tmp_path):
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    with _browser_ingress_client(monkeypatch, tmp_path) as (client, _data_dir), _trusted_identity():
        uploaded = client.post(
            "/api/chat/upload",
            files={"file": ("draft.txt", io.BytesIO(b"alice draft"), "text/plain")},
            headers=_headers("alice"),
        )
        upload_id = uploaded.json()["upload_id"]

        refused = client.delete(f"/api/chat/upload?id={upload_id}", headers=_headers("bob"))
        still_there = client.get(f"/{uploaded.json()['url']}", headers=_headers("alice"))

    assert refused.status_code == 404
    assert still_there.status_code == 200
    assert still_there.content == b"alice draft"
