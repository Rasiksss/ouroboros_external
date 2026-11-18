"""``file_write`` — spec §19.3's ``write``: writes arbitrary content, not only code."""

from __future__ import annotations

import pathlib
from typing import Any

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.egress.workstation import WorkstationError
from ouroboros.storage.attachment_validation import attachment_mime_for_suffix
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.blob.protocol import ObjectTooLargeError
from ouroboros.storage.chat_attachments import read_attachment_bytes
from ouroboros.tools.service.artifacts import resolve_artifact
from ouroboros.tools.workstation._shared import render_error, unavailable_result

_SCHEMA = ToolSchema(
    name="file_write",
    description=(
        "Write content to a file. Creates the file if it doesn't exist, overwrites if it "
        "does. Automatically creates parent directories. Use file_write only for new files "
        "or complete rewrites. "
        "Provide 'content' to write a text string, or 'data_path' to copy a file from the "
        "local data drive (uploads/<name>) into the sandbox as raw bytes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write (relative or absolute)"},
            "content": {
                "type": "string",
                "description": "Text content to write (default). Mutually exclusive with data_path.",
            },
            "data_path": {
                "type": "string",
                "description": "Local data-drive path (uploads/<name>) whose raw bytes to copy into the sandbox. Mutually exclusive with content.",
            },
        },
        "required": ["path", "content"],
    },
)


def _read_drive_bytes(ctx: ToolContext, data_path: str) -> tuple[bytes, str] | str:
    norm = str(data_path or "").strip()
    if not norm:
        return "⚠️ FILE_WRITE_ERROR: 'data_path' is required when 'content' is omitted."
    if not pathlib.PurePosixPath(norm).parts or pathlib.PurePosixPath(norm).parts[0] != "uploads":
        return f"⚠️ FILE_WRITE_ERROR: data_path must live under uploads/: {norm!r}"

    try:
        ctx.drive_path(norm)
    except ValueError:
        return f"⚠️ FILE_WRITE_ERROR: data_path escapes the data drive: {norm!r}"
    try:
        metadata = resolve_artifact(ctx, norm)
        if metadata is not None:
            mime = (
                metadata.get("mime")
                or attachment_mime_for_suffix(metadata.get("filename") or "")
                or "application/octet-stream"
            )
            data = read_attachment_bytes(
                ctx.scope, get_storage_provider(), metadata.get("storage_key") or "", mime=mime
            )
            if data is not None:
                return data, mime
    except ObjectTooLargeError as exc:
        return f"⚠️ FILE_WRITE_ERROR: {exc}"

    return f"⚠️ FILE_WRITE_ERROR: {norm!r} not found in the data drive."


def _handle_file_write(ctx: ToolContext, path: str = "", content: str = "", data_path: str = "", **_kwargs: Any) -> str:
    handle = ctx.workstation
    if handle is None:
        return unavailable_result("file_write")
    if not str(path or "").strip():
        return "⚠️ FILE_WRITE_ERROR: 'path' is required."

    if str(data_path or "").strip():
        if str(content or ""):
            return "⚠️ FILE_WRITE_ERROR: 'content' and 'data_path' are mutually exclusive."
        loaded = _read_drive_bytes(ctx, data_path)
        if isinstance(loaded, str):
            return loaded
        data, mime = loaded
    else:
        data = content.encode("utf-8")
        mime = "text/plain; charset=utf-8"

    try:
        handle.files.write_file(path, data, mime=mime)
    except WorkstationError as exc:
        return render_error("file_write", exc)

    return f"OK: wrote {path} ({len(data)} bytes)"


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="file_write",
            schema=_SCHEMA,
            handler=_handle_file_write,
            timeout_sec=60,
        ),
    ]


__all__ = ["_read_drive_bytes", "get_tools"]
