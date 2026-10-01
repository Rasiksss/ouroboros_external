from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

from new_core.tools.context import ToolContext
from ouroboros.storage.blob.factory import create_storage_provider
from ouroboros.storage.chat_attachments import store_attachment_bytes


def _stored_attachment(tmp_path, *, attachment_id: str, filename: str, mime: str, data: bytes):
    drive_root = tmp_path / "data"
    provider = create_storage_provider(data_dir=drive_root)
    metadata = store_attachment_bytes(
        provider,
        attachment_id=attachment_id,
        filename=filename,
        mime=mime,
        data=data,
        source="browser",
        status="ready",
        message_id="mid-1",
    )
    return drive_root, metadata


def test_attachment_storage_does_not_upload_blob_when_initial_metadata_write_fails():
    from ouroboros.storage.chat_attachments import store_attachment_fileobj

    class FailingMetadataProvider:
        def __init__(self):
            self.blobs = {}

        def get_json(self, _scope, _key):
            return None

        def list_keys(self, _scope, _prefix, **_kwargs):
            return []

        def put_fileobj(self, _scope, key, fileobj, *, content_type):
            del content_type
            self.blobs[key] = fileobj.read()

        def create_json_if_absent(self, _scope, _key, _value):
            raise OSError("metadata unavailable")

    provider = FailingMetadataProvider()

    with pytest.raises(OSError, match="metadata unavailable"):
        store_attachment_fileobj(
            provider,
            attachment_id="attachment-failed",
            filename="report.txt",
            mime="text/plain",
            size=6,
            fileobj=BytesIO(b"report"),
            source="browser",
        )

    assert provider.blobs == {}


def test_attachment_storage_keeps_ttl_metadata_when_blob_rollback_fails():
    from ouroboros.storage.chat_attachments import store_attachment_fileobj

    class Provider:
        def __init__(self):
            self.blobs = {}
            self.metadata = {}
            self.writes = 0

        def atomic_put_json(self, _scope, key, value):
            self.writes += 1
            if self.writes > 1:
                raise OSError("metadata unavailable")
            self.metadata[key] = dict(value)

        def create_json_if_absent(self, scope, key, value):
            self.atomic_put_json(scope, key, value)
            return True

        def replace_json_if_matches(self, scope, key, _expected, replacement):
            self.atomic_put_json(scope, key, replacement)
            return True

        def get_json(self, _scope, _key):
            return None

        def list_keys(self, _scope, _prefix, **_kwargs):
            return []

        def put_fileobj(self, _scope, key, fileobj, *, content_type):
            del content_type
            self.blobs[key] = fileobj.read()

        def delete(self, _scope, _key):
            raise OSError("blob unavailable")

    provider = Provider()

    with pytest.raises(OSError, match="metadata unavailable"):
        store_attachment_fileobj(
            provider,
            attachment_id="attachment-failed",
            filename="report.txt",
            mime="text/plain",
            size=6,
            fileobj=BytesIO(b"report"),
            source="browser",
        )

    assert provider.blobs
    assert [item["status"] for item in provider.metadata.values()] == ["staging"]


def test_attachment_storage_enforces_actual_stream_size(monkeypatch, tmp_path):
    import ouroboros.storage.chat_attachments as attachments

    monkeypatch.setattr(attachments, "MAX_DOCUMENT_BYTES", 4)
    provider = create_storage_provider(data_dir=tmp_path)

    with pytest.raises(attachments.AttachmentError, match="exceeds"):
        attachments.store_attachment_fileobj(
            provider,
            attachment_id="attachment-large",
            filename="report.txt",
            mime="text/plain",
            size=1,
            fileobj=BytesIO(b"12345"),
            source="browser",
        )


def test_attachment_read_streams_current_text_attachment_without_get_bytes(tmp_path, monkeypatch):
    import ouroboros.tools.attachments as attachments

    drive_root, metadata = _stored_attachment(
        tmp_path,
        attachment_id="attachment-1",
        filename="notes.txt",
        mime="text/plain",
        data="строка 1\nстрока 2".encode("utf-16"),
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive_root, attachments=[metadata])
    provider = create_storage_provider(data_dir=drive_root)

    class CopyOnlyProvider:
        def __getattr__(self, name):
            if name == "get_bytes":
                raise AssertionError("attachment_read must not load the whole object")
            return getattr(provider, name)

    monkeypatch.setattr(attachments, "create_storage_provider", lambda **_kwargs: CopyOnlyProvider())

    result = attachments._attachment_read(ctx, "attachment-1")

    assert "строка 1" in result
    assert "строка 2" in result


def test_attachment_read_rejects_attachment_outside_current_task(tmp_path):
    from ouroboros.tools.attachments import _attachment_read

    drive_root, _metadata = _stored_attachment(
        tmp_path,
        attachment_id="attachment-2",
        filename="secret.txt",
        mime="text/plain",
        data=b"secret",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive_root, attachments=[])

    result = _attachment_read(ctx, "attachment-2")

    assert "current task" in result.lower()
    assert "secret" not in result


def test_vlm_query_streams_current_image_by_attachment_id(tmp_path, monkeypatch):
    from ouroboros.tools.vision import _vlm_query

    png = b"\x89PNG\r\n\x1a\n" + b"payload"
    drive_root, metadata = _stored_attachment(
        tmp_path,
        attachment_id="attachment-image",
        filename="image.png",
        mime="image/png",
        data=png,
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive_root, attachments=[metadata])
    provider = create_storage_provider(data_dir=drive_root)

    class CopyOnlyProvider:
        def __getattr__(self, name):
            if name == "get_bytes":
                raise AssertionError("vlm_query must enforce the streaming image limit")
            return getattr(provider, name)

    monkeypatch.setattr(
        "ouroboros.storage.blob.factory.create_storage_provider",
        lambda **_kwargs: CopyOnlyProvider(),
    )
    mock_client = type(
        "Client",
        (),
        {"vision_query": lambda self, **kwargs: (kwargs["images"][0]["base64"], {})},
    )()

    with patch("ouroboros.tools.vision._get_llm_client", return_value=mock_client):
        result = _vlm_query(ctx, prompt="describe", attachment_id="attachment-image")

    assert result
    assert "iVBOR" in result


def test_vlm_query_rejects_attachment_outside_current_task(tmp_path):
    from ouroboros.tools.vision import _vlm_query

    drive_root, _metadata = _stored_attachment(
        tmp_path,
        attachment_id="attachment-image-2",
        filename="image.png",
        mime="image/png",
        data=b"\x89PNG\r\n\x1a\n",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive_root, attachments=[])

    result = _vlm_query(ctx, prompt="describe", attachment_id="attachment-image-2")

    assert "current task" in result.lower()


@pytest.mark.parametrize(
    ("filename", "mime"),
    [
        ("archive.zip", "application/zip"),
        ("legacy.xls", "application/vnd.ms-excel"),
        ("page.html", "text/html"),
        ("binary.exe", "application/octet-stream"),
    ],
)
def test_attachment_validation_rejects_forbidden_formats(tmp_path, filename, mime):
    from ouroboros.storage.chat_attachments import AttachmentError
    from ouroboros.tools.attachments import validate_attachment_path

    path = tmp_path / filename
    path.write_bytes(b"PK\x03\x04payload")

    with pytest.raises(AttachmentError) as exc:
        validate_attachment_path(path, filename, mime)

    assert exc.value.code == "attachment_type_not_allowed"
    assert exc.value.filename == filename


def test_attachment_validation_allows_separate_outbound_image_limit(tmp_path, monkeypatch):
    from ouroboros.tools import attachments

    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(attachments, "MAX_IMAGE_BYTES", 1)

    validated = attachments.validate_attachment_path(
        path,
        path.name,
        "image/png",
        image_max_bytes=16,
    )

    assert validated.kind == "image"


def _ooxml(path: Path, *, member: str, payload: bytes = b"<root/>") -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("_rels/.rels", b"<Relationships/>")
        archive.writestr(member, payload)


def test_ooxml_preflight_accepts_expected_xlsx_members(tmp_path):
    from ouroboros.tools.attachments import _validate_ooxml_package

    path = tmp_path / "book.xlsx"
    _ooxml(path, member="xl/workbook.xml")

    _validate_ooxml_package(
        path,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def test_ooxml_preflight_rejects_extreme_compression_ratio(tmp_path):
    from ouroboros.storage.chat_attachments import AttachmentError
    from ouroboros.tools.attachments import _validate_ooxml_package

    path = tmp_path / "book.xlsx"
    _ooxml(path, member="xl/workbook.xml", payload=b"0" * (2 * 1024 * 1024))

    with pytest.raises(AttachmentError, match="compression ratio"):
        _validate_ooxml_package(
            path,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


def test_ooxml_preflight_rejects_parent_traversal(tmp_path):
    from ouroboros.storage.chat_attachments import AttachmentError
    from ouroboros.tools.attachments import _validate_ooxml_package

    path = tmp_path / "document.docx"
    _ooxml(path, member="word/document.xml")
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("../escape", b"bad")

    with pytest.raises(AttachmentError, match="unsafe path"):
        _validate_ooxml_package(
            path,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )


def test_parser_process_enforces_wall_timeout(tmp_path):
    import threading

    import ouroboros.tools.attachments as attachments
    from ouroboros.storage.chat_attachments import AttachmentError
    from ouroboros.tools.attachments import run_parser

    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")

    with pytest.raises(AttachmentError, match="timed out"):
        run_parser(path, "text/plain", mode="extract", timeout_sec=0)

    errors = []

    def parse_while_busy():
        try:
            run_parser(path, "text/plain", mode="extract", timeout_sec=0.05)
        except AttachmentError as exc:
            errors.append(exc)

    attachments._PARSER_SLOT.acquire()
    worker = threading.Thread(target=parse_while_busy)
    try:
        worker.start()
        worker.join(timeout=0.2)
        assert not worker.is_alive()
    finally:
        attachments._PARSER_SLOT.release()
        worker.join(timeout=1)
    assert errors and "timed out" in str(errors[0])


def test_parser_process_truncates_output_with_marker(tmp_path):
    from ouroboros.tools.attachments import run_parser

    path = tmp_path / "notes.txt"
    path.write_text("x" * 100_500, encoding="utf-8")

    result = run_parser(path, "text/plain", mode="extract")

    assert len(result) <= 100_000
    assert result.endswith("\n[TRUNCATED]")
