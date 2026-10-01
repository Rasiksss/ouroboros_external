"""Compatibility coverage for send_photo and image MIME detection."""

import base64
from unittest.mock import Mock

import pytest

from new_core.tools.context import ToolContext
from ouroboros.tools.core import _detect_image_mime, _send_photo, get_tools


def _make_ctx(tmp_path, chat_id=0, source="browser"):
    workspace = tmp_path / "data" / "tmp" / "task-workspace"
    workspace.mkdir(parents=True)
    return ToolContext(
        repo_dir=tmp_path,
        drive_root=tmp_path / "data",
        current_chat_id=chat_id,
        current_task_source=source,
        task_workspace=workspace,
        media_senders={"image": Mock(return_value=(True, "ok"))},
    )


class TestSendPhotoFilePath:
    def test_file_path_reads_png(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        png = ctx.task_workspace / "test.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        result = _send_photo(ctx, file_path=str(png), caption="test shot")

        assert result.startswith("OK:")
        args, kwargs = ctx.media_senders["image"].call_args
        assert args[0] == 0
        assert kwargs["mime"] == "image/png"
        assert kwargs["source"] == "browser"

    def test_no_input_returns_error(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        result = _send_photo(ctx)
        assert "Provide either" in result


class TestSendPhotoBase64Fallback:
    def test_base64_still_uses_image_event(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200).decode()
        result = _send_photo(ctx, image_base64=b64)
        assert "OK" in result
        assert ctx.media_senders["image"].call_count == 1

    def test_last_screenshot_sentinel_uses_browser_state(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200).decode()
        ctx.browser_state.last_screenshot_b64 = b64

        result = _send_photo(ctx, image_base64="__last_screenshot__")

        assert "OK" in result
        assert ctx.media_senders["image"].call_count == 1

    def test_schema_documents_last_screenshot_sentinel(self):
        send_photo = next(tool for tool in get_tools() if tool.name == "send_photo")
        image_base64 = send_photo.schema.parameters["properties"]["image_base64"]

        assert "__last_screenshot__" in send_photo.schema.description
        assert "__last_screenshot__" in image_base64["description"]


class TestDetectImageMime:
    @pytest.mark.parametrize(
        "payload,expected",
        [
            pytest.param(b"\x89PNG\r\n\x1a\n\x00", "image/png", id="png"),
            pytest.param(b"\xff\xd8\xff\xe0", "image/jpeg", id="jpeg"),
            pytest.param(b"GIF89a", "image/gif", id="gif"),
            pytest.param(b"RIFF\x00\x00\x00\x00WEBP", "image/webp", id="webp"),
            pytest.param(b"\x00\x00\x00\x00", "application/octet-stream", id="unknown"),
        ],
    )
    def test_detect_image_mime(self, payload, expected):
        assert _detect_image_mime(payload) == expected
