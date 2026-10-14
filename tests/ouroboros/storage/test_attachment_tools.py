from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import replace
from io import BytesIO
from unittest.mock import patch

import pytest

from ouroboros.storage.blob.factory import get_storage_provider
from tests.helpers.files import stored_file
from tests.helpers.shared import TEST_SCOPE, make_tool_ctx, scope_with


def test_attachment_parser_import_does_not_require_application_bootstrap():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['ouroboros.app.config'] = None; import ouroboros.tools.attachments, ouroboros.tools.vision",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def _stored_attachment(tmp_path, *, attachment_id: str, filename: str, mime: str, data: bytes):
    drive_root = tmp_path / "data"
    provider = get_storage_provider()
    metadata = stored_file(
        scope=TEST_SCOPE,
        provider=provider,
        data=data,
        attachment_id=attachment_id,
        filename=filename,
        mime=mime,
        source="browser",
    )
    from tests.helpers.files import link_file

    link_file(metadata)
    return drive_root, metadata


def test_attachment_storage_enforces_actual_stream_size(isolate_settings, tmp_path):
    import ouroboros.storage.chat_attachments as attachments

    isolate_settings.save_settings({"OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES": 4})
    provider = get_storage_provider()

    with pytest.raises(attachments.AttachmentError, match="4 bytes"):
        attachments.store_attachment_fileobj(
            TEST_SCOPE,
            provider,
            attachment_id="attachment-large",
            filename="report.txt",
            mime="text/plain",
            size=1,
            fileobj=BytesIO(b"12345"),
            source="browser",
        )


def test_reachable_attachment_resolves_by_chat_not_by_task(tmp_path):
    from ouroboros.tools.attachments import resolve_reachable_attachment

    drive_root, metadata = _stored_attachment(
        tmp_path,
        attachment_id="attachment-chat",
        filename="notes.txt",
        mime="text/plain",
        data=b"notes",
    )
    same_chat = make_tool_ctx(repo_dir=tmp_path, drive_root=drive_root, attachments=[])
    other_chat = make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=drive_root,
        scope=scope_with(TEST_SCOPE, chat_id="other-chat"),
        attachments=[],
    )

    assert resolve_reachable_attachment(same_chat, "attachment-chat") is not None
    assert resolve_reachable_attachment(other_chat, "attachment-chat") is None
    # Never narrower than the run's own attachments: a bound id resolves whatever chat it names.
    bound = replace(other_chat, attachments=[dict(metadata)])
    assert resolve_reachable_attachment(bound, "attachment-chat") is not None


def test_reachable_attachment_refusal_is_logged(tmp_path, caplog):
    from ouroboros.tools.attachments import resolve_reachable_attachment

    drive_root, _metadata = _stored_attachment(
        tmp_path,
        attachment_id="attachment-logged",
        filename="notes.txt",
        mime="text/plain",
        data=b"notes",
    )
    ctx = make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=drive_root,
        scope=scope_with(TEST_SCOPE, chat_id="other-chat"),
        task_id="task-refused",
        attachments=[],
    )

    with caplog.at_level(logging.INFO):
        assert resolve_reachable_attachment(ctx, "attachment-logged") is None
        assert resolve_reachable_attachment(ctx, "attachment-absent") is None

    assert "event=attachment_reach_refused task_id=task-refused reason=not_reachable" in caplog.text


def test_attachment_read_streams_current_text_attachment_without_get_bytes(tmp_path, monkeypatch):
    import ouroboros.tools.attachments as attachments

    drive_root, metadata = _stored_attachment(
        tmp_path,
        attachment_id="attachment-1",
        filename="notes.txt",
        mime="text/plain",
        data="строка 1\nстрока 2".encode("utf-16"),
    )
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=drive_root, attachments=[metadata])
    provider = get_storage_provider()

    class CopyOnlyProvider:
        def __getattr__(self, name):
            if name == "get_bytes":
                raise AssertionError("attachment_read must not load the whole object")
            return getattr(provider, name)

    monkeypatch.setattr(attachments, "get_storage_provider", lambda **_kwargs: CopyOnlyProvider())

    result = attachments._attachment_read(ctx, "attachment-1")

    assert "строка 1" in result
    assert "строка 2" in result


def test_attachment_read_uses_configured_document_limit(tmp_path, isolate_settings):
    from ouroboros.tools.attachments import _attachment_read

    drive_root, metadata = _stored_attachment(
        tmp_path, attachment_id="attachment-limit", filename="notes.txt", mime="text/plain", data=b"12345"
    )
    isolate_settings.save_settings({"OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES": 4})
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=drive_root, attachments=[metadata])
    assert _attachment_read(ctx, metadata["id"]) == "Attachment is too large to parse. Max 4 bytes."


@pytest.mark.parametrize("code,retryable", [("attachment_parser_unavailable", True), ("invalid_attachment", False)])
@pytest.mark.parametrize("tool_name,argument", [("attachment_read", "attachment_id"), ("send_file", "file_path")])
def test_attachment_parser_failure_is_recorded_as_failed_tool(
    tmp_path, monkeypatch, code, retryable, tool_name, argument
):
    import json

    from new_core.llm.types import ToolCall, ToolCallFunction
    from new_core.tools.providers.builtin import BuiltinToolProvider
    from ouroboros.storage.attachment_validation import AttachmentError
    from tests.helpers.shared import make_catalog, run_tool_calls

    drive_root, metadata = _stored_attachment(
        tmp_path, attachment_id="parser-failure", filename="notes.txt", mime="text/plain", data=b"notes"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=drive_root, attachments=[metadata], task_workspace=workspace)
    catalog = make_catalog([BuiltinToolProvider(modules=("attachments", "service.messaging"))])

    def unavailable(*_args, **_kwargs):
        raise AttachmentError(code, "parser failed", retryable=retryable)

    monkeypatch.setattr("ouroboros.storage.attachment_validation.run_parser", unavailable)
    call = ToolCall(
        id="read-failure",
        function=ToolCallFunction(name=tool_name, arguments=json.dumps({argument: metadata["id"]})),
    )
    messages, trace = [], {"tool_calls": []}
    errors = run_tool_calls(
        [call], catalog, drive_root / "logs", "task-1", messages, trace, lambda _text: None, ctx=ctx
    )

    assert errors == 1
    outcome = trace["tool_calls"][-1]
    assert outcome["is_error"] is True
    assert outcome["status"] == "error"
    assert outcome["error_code"] == f"TOOL_{code.upper()}"
    assert f"retryable={str(retryable).lower()}" in messages[-1]["content"]
    assert "parser failed" in messages[-1]["content"]


def test_attachment_read_accepts_earlier_attachment_of_the_same_chat(tmp_path):
    from ouroboros.tools.attachments import _attachment_read

    drive_root, _metadata = _stored_attachment(
        tmp_path,
        attachment_id="attachment-2",
        filename="notes.txt",
        mime="text/plain",
        data="прошлое сообщение".encode(),
    )
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=drive_root, attachments=[])

    assert "прошлое сообщение" in _attachment_read(ctx, "attachment-2")


def test_attachment_read_rejects_attachment_of_another_chat(tmp_path):
    from ouroboros.tools.attachments import _attachment_read

    drive_root, _metadata = _stored_attachment(
        tmp_path,
        attachment_id="attachment-3",
        filename="secret.txt",
        mime="text/plain",
        data=b"secret",
    )
    ctx = make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=drive_root,
        scope=scope_with(TEST_SCOPE, chat_id="another-chat"),
        attachments=[],
    )

    result = _attachment_read(ctx, "attachment-3")

    assert "not found" in result.lower()
    assert "secret" not in result


@pytest.mark.parametrize("source", ["attachment_id", "file_path"])
def test_vlm_query_streams_owned_image(tmp_path, monkeypatch, source):
    from ouroboros.storage.blob.drive import DriveStorage
    from ouroboros.tools.vision import _vlm_query

    png = b"\x89PNG\r\n\x1a\n" + b"payload"
    drive_root, metadata = _stored_attachment(
        tmp_path,
        attachment_id="attachment-image",
        filename="image.png",
        mime="image/png",
        data=png,
    )
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=drive_root, attachments=[metadata])
    provider = get_storage_provider()

    class CopyOnlyProvider:
        def __getattr__(self, name):
            if name == "get_bytes":
                raise AssertionError("vlm_query must enforce the streaming image limit")
            return getattr(provider, name)

    monkeypatch.setattr(
        "ouroboros.storage.blob.factory.get_storage_provider",
        lambda **_kwargs: CopyOnlyProvider(),
    )
    monkeypatch.setattr(
        "ouroboros.storage.blob.drive.get_drive_storage",
        lambda _root: DriveStorage(drive_root, CopyOnlyProvider()),
    )
    monkeypatch.setattr("ouroboros.tools.vision._allowed_file_roots", lambda: [drive_root / "uploads"])
    mock_client = type(
        "Client",
        (),
        {"vision_query": lambda self, **kwargs: (kwargs["images"][0]["base64"], {})},
    )()

    with patch("ouroboros.tools.vision.LLMClient", return_value=mock_client):
        value = metadata["id"] if source == "attachment_id" else str(drive_root / "uploads" / metadata["storage_key"])
        result = _vlm_query(ctx, prompt="describe", **{source: value})

    assert result
    assert "iVBOR" in result


def test_vlm_query_accepts_earlier_image_of_the_same_chat(tmp_path):
    from ouroboros.tools.vision import _vlm_query

    drive_root, _metadata = _stored_attachment(
        tmp_path,
        attachment_id="attachment-image-3",
        filename="image.png",
        mime="image/png",
        data=b"\x89PNG\r\n\x1a\n" + b"payload",
    )
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=drive_root, attachments=[])
    mock_client = type(
        "Client",
        (),
        {"vision_query": lambda self, **kwargs: (kwargs["images"][0]["base64"], {})},
    )()

    with patch("ouroboros.tools.vision.LLMClient", return_value=mock_client):
        result = _vlm_query(ctx, prompt="describe", attachment_id="attachment-image-3")

    assert "iVBOR" in result


def test_vlm_query_rejects_attachment_of_another_chat(tmp_path):
    from ouroboros.tools.vision import _vlm_query

    drive_root, _metadata = _stored_attachment(
        tmp_path,
        attachment_id="attachment-image-2",
        filename="image.png",
        mime="image/png",
        data=b"\x89PNG\r\n\x1a\n",
    )
    ctx = make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=drive_root,
        scope=scope_with(TEST_SCOPE, chat_id="another-chat"),
        attachments=[],
    )

    result = _vlm_query(ctx, prompt="describe", attachment_id="attachment-image-2")

    assert "not found" in result.lower()


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
    from ouroboros.storage.attachment_validation import AttachmentError, validate_attachment_path

    path = tmp_path / filename
    path.write_bytes(b"PK\x03\x04payload")

    with pytest.raises(AttachmentError) as exc:
        validate_attachment_path(path, filename, mime)

    assert exc.value.code == "attachment_type_not_allowed"
    assert exc.value.filename == filename


@pytest.mark.parametrize("limit", [4, 7])
def test_legacy_image_staging_checks_encoded_and_actual_size(tmp_path, isolate_settings, limit):
    import base64

    from ouroboros.storage.attachment_validation import AttachmentError
    from ouroboros.storage.chat_attachments import store_legacy_image_base64

    isolate_settings.save_settings({"OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES": limit})
    provider = get_storage_provider()
    with pytest.raises(AttachmentError, match=f"{limit} bytes"):
        store_legacy_image_base64(
            TEST_SCOPE,
            provider,
            attachment_id="legacy-large",
            image_base64=base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(),
            claimed_mime="image/png",
            source="browser",
        )


@pytest.mark.parametrize(
    "source,limit", [("attachment_id", 7), ("file_path", 7), ("image_base64", 4), ("image_base64", 7)]
)
def test_vlm_query_uses_configured_image_limit(tmp_path, isolate_settings, monkeypatch, source, limit):
    import base64

    from ouroboros.tools.vision import _vlm_query

    drive_root, metadata = _stored_attachment(
        tmp_path, attachment_id="image-limit", filename="image.png", mime="image/png", data=b"\x89PNG\r\n\x1a\n"
    )
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=drive_root, attachments=[metadata])
    monkeypatch.setattr("ouroboros.tools.vision._allowed_file_roots", lambda: [drive_root / "uploads"])
    isolate_settings.save_settings({"OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES": limit})
    value = {
        "attachment_id": metadata["id"],
        "file_path": str(drive_root / "uploads" / metadata["storage_key"]),
        "image_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(),
    }[source]
    with patch("ouroboros.tools.vision.LLMClient") as get_client:
        assert f"Max {limit} bytes" in _vlm_query(ctx, prompt="describe", **{source: value})
        get_client.assert_not_called()
